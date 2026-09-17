from fastapi.testclient import TestClient

from backend.api.main import create_app

# No "with" here: entering TestClient as a context manager runs the app's
# lifespan (which starts the outbox publisher / Kafka producer). /health and
# /openapi.json don't depend on that, and shouldn't require Kafka to be up.
client = TestClient(create_app())


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_schema_is_served() -> None:
    assert client.get("/openapi.json").status_code == 200
