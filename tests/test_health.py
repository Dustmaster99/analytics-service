import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app import app

def test_health():
    client = app.test_client()
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}