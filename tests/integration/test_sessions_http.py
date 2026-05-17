from __future__ import annotations

import threading
import time


def test_create_session_returns_201_with_session_metadata(session_http_client):
    response = session_http_client.post("/sessions")
    assert response.status_code == 201

    body = response.json()
    assert "session_id" in body
    assert len(body["session_id"]) == 32  # uuid4().hex
    assert "created_at" in body
    assert "last_used" in body
    assert body["created_at"] == body["last_used"]


def test_list_sessions_includes_newly_created(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]

    listed = session_http_client.get("/sessions").json()

    assert any(s["session_id"] == sid for s in listed["sessions"])


def test_get_session_returns_metadata(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]

    response = session_http_client.get(f"/sessions/{sid}")

    assert response.status_code == 200
    assert response.json()["session_id"] == sid


def test_delete_session_removes_from_registry_and_returns_204(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]

    response = session_http_client.delete(f"/sessions/{sid}")
    assert response.status_code == 204

    listed = session_http_client.get("/sessions").json()
    assert not any(s["session_id"] == sid for s in listed["sessions"])

    # subsequent get on the same id is 404
    assert session_http_client.get(f"/sessions/{sid}").status_code == 404


def test_session_execute_runs_code(session_http_client):
    sid = session_http_client.post("/sessions").json()["session_id"]

    response = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "print(42)"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == "42\n"
    assert body["exit_code"] == 0
    assert body["timed_out"] is False


def test_session_execute_state_persists_across_calls(session_http_client):
    """§6.4 acceptance through the HTTP layer."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    setup = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "x = 100"},
    )
    assert setup.status_code == 200

    follow_up = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "print(x + 1)"},
    )
    assert follow_up.status_code == 200
    assert follow_up.json()["stdout"].strip() == "101"


def test_unknown_session_returns_404_on_get_delete_execute(session_http_client):
    fake = "0" * 32

    assert session_http_client.get(f"/sessions/{fake}").status_code == 404
    assert session_http_client.delete(f"/sessions/{fake}").status_code == 404
    assert (
        session_http_client.post(
            f"/sessions/{fake}/execute",
            json={"code": "print(1)"},
        ).status_code
        == 404
    )


def test_terminated_session_returns_410_on_subsequent_execute(session_http_client):
    """Substep-2B: SystemExit ends the kernel. First execute raises
    SessionTerminated (mapped to 410); subsequent calls also 410."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    first = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "import sys; sys.exit(0)"},
    )
    assert first.status_code == 410

    second = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={"code": "print('after')"},
    )
    assert second.status_code == 410


def test_concurrent_execute_returns_409(session_http_client):
    """Substep-1 decision 2: a second concurrent execute on the same
    session is rejected with HTTP 409 + {'error': 'session_busy'}."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    results: dict[str, object] = {}

    def call(label: str, code: str) -> None:
        results[label] = session_http_client.post(
            f"/sessions/{sid}/execute",
            json={"code": code},
        )

    # The slow execute holds the lock for ~2 seconds.
    t1 = threading.Thread(target=call, args=("slow", "import time; time.sleep(2)"))
    t1.start()
    time.sleep(0.5)  # let the slow call acquire the lock

    t2 = threading.Thread(target=call, args=("fast", "print('hi')"))
    t2.start()

    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["slow"].status_code == 200
    assert results["fast"].status_code == 409
    assert results["fast"].json() == {"error": "session_busy"}


def test_session_endpoints_require_auth_when_key_set(session_http_client_authed):
    """With dev_api_key configured, /sessions/* returns 401 without a bearer
    and 200 with the correct one."""
    no_auth = session_http_client_authed.get("/sessions")
    assert no_auth.status_code == 401

    authed = session_http_client_authed.get(
        "/sessions",
        headers={"Authorization": "Bearer test-secret-12345"},
    )
    assert authed.status_code == 200


def test_session_execute_response_carries_rich_outputs(session_http_client):
    """Substep 7: POST /sessions/{id}/execute returns a SessionExecuteResponse
    with outputs and dropped_outputs in the JSON body."""
    sid = session_http_client.post("/sessions").json()["session_id"]

    response = session_http_client.post(
        f"/sessions/{sid}/execute",
        json={
            "code": (
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3])"
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert "outputs" in body
    assert "dropped_outputs" in body
    assert len(body["outputs"]) == 1
    assert body["outputs"][0]["type"] == "plot"
    assert body["outputs"][0]["mime_type"] == "image/png"
    assert body["outputs"][0]["data"]
    assert body["dropped_outputs"] == []