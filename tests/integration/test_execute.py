# import pytest
from fastapi.testclient import TestClient

from kestrel.config import Settings, get_settings
from kestrel.execution.manager import run_code

def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_execute_success(client: TestClient) -> None:
    response = client.post("/execute", json={"code": "print(2 + 2)"})

    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == "4\n"
    assert body["stderr"] == ""
    assert body["exit_code"] == 0
    assert body["timed_out"] is False
    assert body["stdout_truncated"] is False
    assert body["stderr_truncated"] is False

def test_execute_validation_rejects_empty_code(client: TestClient) -> None:
    response = client.post("/execute", json={"code": ""})

    assert response.status_code == 422
    detail = response.json()["detail"]
    # Pydantic's error structure: a list of dicts with "loc" tuples pointing to the bad field.
    assert any("code" in error["loc"] for error in detail)

def test_execute_timeout(client: TestClient) -> None:
    def override_settings() -> Settings:
        return Settings(
            dev_api_key="",
            execute_timeout_seconds=0.5,
            execute_output_cap_bytes=1_048_576,
            log_level="INFO",
            log_json=False,
        )

    client.app.dependency_overrides[get_settings] = override_settings
    try:
        response = client.post("/execute", json={"code": "while True: pass"})
    finally:
        client.app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == -1
    assert body["duration_ms"] >= 500  # at least the timeout we set


async def test_run_code_truncates_stdout() -> None:
    settings = Settings(
        dev_api_key="",
        execute_timeout_seconds=5.0,
        execute_output_cap_bytes=1024,
        log_level="INFO",
        log_json=False,
    )

    result = await run_code('print("x" * 100_000)', settings)

    assert len(result.stdout.encode("utf-8")) == 1024
    assert result.stdout_truncated is True
    assert result.exit_code == 0
    assert result.timed_out is False