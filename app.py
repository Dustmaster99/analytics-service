import os
import sys
import threading
import json
import uuid
import time
import logging
import boto3

from botocore.exceptions import NoCredentialsError, ClientError
from flask import Flask, jsonify
from dotenv import load_dotenv

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor


# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

load_dotenv()


# --- OpenTelemetry Config ---
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "analytics-worker")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-collector.monitoring.svc.cluster.local:4318"
)

resource = Resource.create({
    ResourceAttributes.SERVICE_NAME: SERVICE_NAME,
    "service.version": os.getenv("SERVICE_VERSION", "1.0.0"),
    "deployment.environment": os.getenv("ENVIRONMENT", "dev"),
})

# Traces
trace_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces"
)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(__name__)

# Metrics
metric_exporter = OTLPMetricExporter(
    endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics"
)
metric_reader = PeriodicExportingMetricReader(
    exporter=metric_exporter,
    export_interval_millis=10000
)
metrics_provider = MeterProvider(
    resource=resource,
    metric_readers=[metric_reader]
)
metrics.set_meter_provider(metrics_provider)
meter = metrics.get_meter(__name__)

messages_received_counter = meter.create_counter(
    name="sqs_messages_received_total",
    description="Total de mensagens recebidas da fila SQS",
    unit="1"
)

messages_processed_counter = meter.create_counter(
    name="sqs_messages_processed_total",
    description="Total de mensagens processadas com sucesso",
    unit="1"
)

messages_failed_counter = meter.create_counter(
    name="sqs_messages_failed_total",
    description="Total de mensagens que falharam no processamento",
    unit="1"
)

message_processing_duration = meter.create_histogram(
    name="sqs_message_processing_duration_seconds",
    description="Tempo de processamento de mensagens SQS",
    unit="s"
)

last_processed_timestamp = 0


def observe_last_processed_timestamp(options):
    yield metrics.Observation(
        last_processed_timestamp,
        {}
    )


meter.create_observable_gauge(
    name="sqs_last_processed_timestamp",
    callbacks=[observe_last_processed_timestamp],
    description="Timestamp Unix da última mensagem processada com sucesso",
    unit="s"
)

# Instrumentação automática do boto3/botocore
BotocoreInstrumentor().instrument()


# --- Configuração AWS ---
AWS_REGION = os.getenv("AWS_REGION")
SQS_QUEUE_URL = os.getenv("AWS_SQS_URL")
DYNAMODB_TABLE_NAME = os.getenv("AWS_DYNAMODB_TABLE")

if not all([AWS_REGION, SQS_QUEUE_URL, DYNAMODB_TABLE_NAME]):
    log.critical("Erro: AWS_REGION, AWS_SQS_URL, e AWS_DYNAMODB_TABLE devem ser definidos.")
    sys.exit(1)

try:
    session = boto3.Session(region_name=AWS_REGION)
    sqs_client = session.client("sqs")
    dynamodb_client = session.client("dynamodb")
    log.info(f"Clientes Boto3 inicializados na região {AWS_REGION}")
except NoCredentialsError:
    log.critical("Credenciais da AWS não encontradas. Verifique seu ambiente.")
    sys.exit(1)
except Exception as e:
    log.critical(f"Erro ao inicializar o Boto3: {e}")
    sys.exit(1)


def process_message(message):
    global last_processed_timestamp

    start_time = time.time()
    message_id = message.get("MessageId", "unknown")

    with tracer.start_as_current_span("process_sqs_message") as span:
        span.set_attribute("messaging.system", "aws_sqs")
        span.set_attribute("messaging.message.id", message_id)
        span.set_attribute("aws.dynamodb.table", DYNAMODB_TABLE_NAME)

        try:
            log.info(f"Processando mensagem ID: {message_id}")
            body = json.loads(message["Body"])

            flag_name = body.get("flag_name", "unknown")
            user_id = body.get("user_id", "unknown")

            span.set_attribute("feature_flag.name", flag_name)
            span.set_attribute("user.id", user_id)

            event_id = str(uuid.uuid4())

            item = {
                "event_id": {"S": event_id},
                "user_id": {"S": body["user_id"]},
                "flag_name": {"S": body["flag_name"]},
                "result": {"BOOL": body["result"]},
                "timestamp": {"S": body["timestamp"]}
            }

            dynamodb_client.put_item(
                TableName=DYNAMODB_TABLE_NAME,
                Item=item
            )

            sqs_client.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=message["ReceiptHandle"]
            )

            duration = time.time() - start_time
            last_processed_timestamp = time.time()

            messages_processed_counter.add(1, {
                "queue": "analytics-events",
                "flag_name": flag_name,
                "status": "success"
            })

            message_processing_duration.record(duration, {
                "queue": "analytics-events",
                "status": "success"
            })

            span.set_attribute("event.id", event_id)
            span.set_status(trace.Status(trace.StatusCode.OK))

            log.info(f"Evento {event_id} salvo no DynamoDB.")

        except json.JSONDecodeError as e:
            duration = time.time() - start_time

            messages_failed_counter.add(1, {
                "queue": "analytics-events",
                "error_type": "json_decode_error"
            })

            message_processing_duration.record(duration, {
                "queue": "analytics-events",
                "status": "error"
            })

            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

            log.error(f"Erro ao decodificar JSON da mensagem ID: {message_id}")

        except ClientError as e:
            duration = time.time() - start_time

            messages_failed_counter.add(1, {
                "queue": "analytics-events",
                "error_type": "aws_client_error"
            })

            message_processing_duration.record(duration, {
                "queue": "analytics-events",
                "status": "error"
            })

            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

            log.error(f"Erro do Boto3 ao processar {message_id}: {e}")

        except Exception as e:
            duration = time.time() - start_time

            messages_failed_counter.add(1, {
                "queue": "analytics-events",
                "error_type": "unexpected_error"
            })

            message_processing_duration.record(duration, {
                "queue": "analytics-events",
                "status": "error"
            })

            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

            log.error(f"Erro inesperado ao processar {message_id}: {e}")


def sqs_worker_loop():
    log.info("Iniciando o worker SQS...")

    while True:
        try:
            response = sqs_client.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20
            )

            messages = response.get("Messages", [])

            if not messages:
                continue

            messages_received_counter.add(len(messages), {
                "queue": "analytics-events"
            })

            log.info(f"Recebidas {len(messages)} mensagens.")

            for message in messages:
                process_message(message)

        except ClientError as e:
            messages_failed_counter.add(1, {
                "queue": "analytics-events",
                "error_type": "sqs_receive_client_error"
            })

            log.error(f"Erro do Boto3 no loop principal do SQS: {e}")
            time.sleep(10)

        except Exception as e:
            messages_failed_counter.add(1, {
                "queue": "analytics-events",
                "error_type": "sqs_receive_unexpected_error"
            })

            log.error(f"Erro inesperado no loop principal do SQS: {e}")
            time.sleep(10)


app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/telemetry")
def telemetry_info():
    return jsonify({
        "service_name": SERVICE_NAME,
        "otel_endpoint": OTEL_EXPORTER_OTLP_ENDPOINT,
        "otlp_traces_path": f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces",
        "otlp_metrics_path": f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics",
        "status": "otel-configured"
    })


@app.route("/app-metrics")
def app_metrics():
    return jsonify({
        "service_name": SERVICE_NAME,
        "last_processed_timestamp": last_processed_timestamp
    })


def start_worker():
    worker_thread = threading.Thread(target=sqs_worker_loop, daemon=True)
    worker_thread.start()


start_worker()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8005))
    app.run(host="0.0.0.0", port=port, debug=False)