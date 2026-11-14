import pytest

from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.settings.workspace import WorkspaceOssSettings


@pytest.fixture
def oss_env(monkeypatch):
    for name, value in {
        "QWENPAW_DATA_WORKSPACE_OSS_BUCKET": "test-bucket",
        "QWENPAW_DATA_WORKSPACE_OSS_PREFIX": "data/.qwenpaw-data",
        "QWENPAW_DATA_WORKSPACE_OSS_ENDPOINT": "https://oss-cn-beijing.aliyuncs.com",
    }.items():
        monkeypatch.setenv(name, value)
    clear_settings()


def test_oss_settings_load(oss_env):
    oss = WorkspaceOssSettings()
    oss.require()
    assert oss.bucket == "test-bucket"
    assert oss.prefix == "data/.qwenpaw-data"
    assert oss.endpoint == "https://oss-cn-beijing.aliyuncs.com"


@pytest.mark.parametrize(
    "name",
    [
        "QWENPAW_DATA_WORKSPACE_OSS_BUCKET",
        "QWENPAW_DATA_WORKSPACE_OSS_PREFIX",
        "QWENPAW_DATA_WORKSPACE_OSS_ENDPOINT",
    ],
)
def test_missing_oss_setting_fails(oss_env, monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(ValueError, match=name):
        WorkspaceOssSettings().require()

