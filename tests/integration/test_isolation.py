from fastapi.testclient import TestClient


def test_isolation_runs_as_nobody(docker_client: TestClient) -> None:
    response = docker_client.post(
        "/execute",
        json={"code": "import os\nprint(os.getuid(), os.getgid())"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "65534 65534"


def test_isolation_network_denied(docker_client: TestClient) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "BLOCKED"


def test_isolation_rootfs_readonly(docker_client: TestClient) -> None:
    code = (
        "try:\n"
        "    open('/etc/test_kestrel', 'w').write('x')\n"
        "    print('WRITE_OK')\n"
        "except OSError:\n"
        "    print('WRITE_BLOCKED')\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "WRITE_BLOCKED"


def test_isolation_no_new_privileges(docker_client: TestClient) -> None:
    code = (
        "with open('/proc/self/status') as f:\n"
        "    for line in f:\n"
        "        if line.startswith('NoNewPrivs:'):\n"
        "            print(line.strip())\n"
        "            break\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "NoNewPrivs:\t1"


def test_isolation_capabilities_dropped(docker_client: TestClient) -> None:
    code = (
        "with open('/proc/self/status') as f:\n"
        "    for line in f:\n"
        "        if line.startswith('CapEff:'):\n"
        "            print(line.strip())\n"
        "            break\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    cap_eff_hex = body["stdout"].strip().split()[1]
    assert int(cap_eff_hex, 16) == 0, f"expected zero effective capabilities, got {cap_eff_hex}"