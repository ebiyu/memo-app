import pytest
from fastapi.testclient import TestClient

from memo.server.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "memo.db"))) as c:
        yield c
