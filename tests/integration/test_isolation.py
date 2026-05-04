import pytest

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

def test_isolation_pids_limit_enforced(docker_client: TestClient) -> None:
    code = (
        "import os\n"
        "import time\n"
        "children = 0\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "    children += 1\n"
        "print(f'FORKED {children}')\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
    assert body["exit_code"] == 0
    forked = int(body["stdout"].strip().split()[1])
    assert 0 < forked < 200, f"expected pids cap to fire before 200 forks, got {forked}"
    assert forked <= 64, f"pids cap should be ~64, got {forked} (cap may have been loosened)"

def test_isolation_seccomp_filter_active(docker_client: TestClient) -> None:
    code = (
        "with open('/proc/self/status') as f:\n"
        "    for line in f:\n"
        "        if line.startswith('Seccomp:'):\n"
        "            print(line.strip())\n"
        "            break\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "Seccomp:\t2"

def test_isolation_tmpfs_size_capped(docker_client: TestClient) -> None:
    code = (
        "import os\n"
        "fd = os.open('/tmp/big', os.O_WRONLY | os.O_CREAT)\n"
        "chunk = b'x' * (1024 * 1024)\n"
        "written = 0\n"
        "blocked = None\n"
        "try:\n"
        "    for _ in range(128):\n"
        "        written += os.write(fd, chunk)\n"
        "except OSError as e:\n"
        "    blocked = e.errno\n"
        "finally:\n"
        "    os.close(fd)\n"
        "print(f'written={written} blocked={blocked}')\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    output = body["stdout"].strip()
    assert "blocked=28" in output, f"expected ENOSPC (errno 28), got: {output}"
    written_str = output.split()[0].split("=")[1]
    written_mib = int(written_str) / (1024 * 1024)
    assert 50 < written_mib <= 64, f"expected ~64 MiB written before cap, got {written_mib} MiB"


def _is_wsl2() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


@pytest.mark.skipif(
    _is_wsl2(),
    reason="WSL2 kernel does not enforce Docker --memory cgroup limits",
)
def test_isolation_memory_limit_enforced(docker_client: TestClient) -> None:
    code = (
        "chunks = []\n"
        "for _ in range(512):\n"
        "    chunks.append(bytearray(1024 * 1024))\n"
        "print(f'ALLOCATED {len(chunks)} MiB')\n"
    )
    response = docker_client.post("/execute", json={"code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False, "OOM-kill should fire before the 5s timeout"
    assert body["exit_code"] != 0, (
        f"expected non-zero exit (OOM-killed), got {body['exit_code']} — "
        f"memory cap not enforced? stdout={body['stdout']!r}"
    )