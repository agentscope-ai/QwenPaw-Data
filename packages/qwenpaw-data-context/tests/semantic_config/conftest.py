import pytest
from context_manager.settings import clear_settings


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QWENPAW_DATA_SECURITY_AUDIT_LOG", raising=False)
    clear_settings()
    yield
    clear_settings()
