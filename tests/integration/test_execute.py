from fastapi.testclient import TestClient

from kestrel.config import Settings, get_settings


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


def test_execute_timeout(client: TestClient, override_settings) -> None:
    override_settings(execute_timeout_seconds=2.0)

    response = client.post("/execute", json={"code": "while True: pass"})

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == -1
    assert body["duration_ms"] >= 2000


def test_execute_truncates_stdout(client: TestClient, override_settings) -> None:
    override_settings(execute_output_cap_bytes=1024)

    response = client.post("/execute", json={"code": 'print("x" * 100_000)'})

    assert response.status_code == 200
    body = response.json()
    assert len(body["stdout"].encode("utf-8")) == 1024
    assert body["stdout_truncated"] is True
    assert body["exit_code"] == 0
    assert body["timed_out"] is False


def test_execute_requires_api_key_when_set(client: TestClient, override_settings) -> None:
    override_settings(dev_api_key="secret123")

    response = client.post("/execute", json={"code": "print(1)"})

    assert response.status_code == 401


def test_execute_accepts_correct_api_key(client: TestClient, override_settings) -> None:
    override_settings(dev_api_key="secret123")

    response = client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer secret123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == "1\n"
    assert body["exit_code"] == 0


def test_execute_rejects_wrong_api_key(client: TestClient, override_settings) -> None:
    override_settings(dev_api_key="secret123")

    response = client.post(
        "/execute",
        json={"code": "print(1)"},
        headers={"Authorization": "Bearer wrongtoken"},
    )

    assert response.status_code == 401


def test_response_carries_request_id_header(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id is not None
    assert len(request_id) > 0


def test_response_echoes_supplied_request_id(client: TestClient) -> None:
    supplied = "test-request-id-123"
    response = client.get("/health", headers={"X-Request-ID": supplied})
    assert response.headers["x-request-id"] == supplied


def test_execute_honors_custom_spool_dir(docker_client: TestClient, tmp_path) -> None:
    # Phase 8 substep 1 (decision 8-api-image): when KESTREL_EXEC_SPOOL_DIR is set,
    # the stateless-execute code tempfile is created under that directory and
    # bind-mounted into the sandbox. The user code reads back its own mounted
    # source at /sandbox/main.py — proving the spool-dir tempfile was created and
    # the bind mount resolved. The default (empty) path is covered by every other
    # execute test above. The docker-out-of-docker host-path-match guarantee is
    # exercised end-to-end by the Compose smoke in slice 3.
    docker_client.app.dependency_overrides[get_settings] = lambda: Settings(
        exec_spool_dir=str(tmp_path)
    )
    code = 'print(open("/sandbox/main.py").read(), end="")'
    response = docker_client.post("/execute", json={"code": code})

    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == code
    assert body["exit_code"] == 0


def test_execute_honors_per_request_timeout(client: TestClient, override_settings) -> None:
    # A per-request timeout below the server ceiling tightens the budget.
    override_settings(execute_timeout_seconds=10.0)

    response = client.post(
        "/execute",
        json={"code": "while True: pass", "timeout_seconds": 1.0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == -1
    assert body["duration_ms"] >= 1000
    assert body["duration_ms"] < 9000  # killed at the 1s request budget, not the 10s ceiling


def test_execute_timeout_clamped_to_server_ceiling(client: TestClient, override_settings) -> None:
    # A per-request value above the ceiling is clamped down — a caller can never
    # exceed the operator-configured maximum.
    override_settings(execute_timeout_seconds=1.0)

    response = client.post(
        "/execute",
        json={"code": "while True: pass", "timeout_seconds": 30.0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == -1
    assert body["duration_ms"] >= 1000
    assert body["duration_ms"] < 10000  # killed near the 1s ceiling, not 30s


def test_execute_rejects_nonpositive_timeout(client: TestClient) -> None:
    response = client.post("/execute", json={"code": "print(1)", "timeout_seconds": 0})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("timeout_seconds" in error["loc"] for error in detail)