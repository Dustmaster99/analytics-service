# =========================
# Stage 1 — Build
# =========================
FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Instala dependências
RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81" \
 && pip install --no-cache-dir -r requirements.txt \
 && python -c "import pkg_resources; print('pkg_resources ok')"


# =========================
# Stage 2 — Runtime
# =========================
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Dependência runtime do PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY app.py .

EXPOSE ${PORT}

CMD ["sh", "-c", ": \"${PORT:?PORT environment variable is required}\" && python app.py --port ${PORT}"]