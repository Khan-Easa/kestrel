import pytest
from fastapi.testclient import TestClient
from collections.abc import Callable
from typing import Any

from kestrel.app import create_app
from kestrel.config import Settings, get_settings

@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)

@pytest.fixture
def override_settings(client: TestClient) -> Callable[..., None]:
    def _apply(**overrides: Any) -> None:
        defaults = {
            "dev_api_key": "",
            "execute_timeout_seconds": 5.0,
            "execute_output_cap_bytes": 1_048_576,
            "log_level": "INFO",
            "log_json": False,
        }
        defaults.update(overrides)
        client.app.dependency_overrides[get_settings] = lambda: Settings(**defaults)

    yield _apply
    client.app.dependency_overrides.clear()
