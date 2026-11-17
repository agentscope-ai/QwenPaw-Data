import os

import pytest

from qwenpaw_data.host.core.api.routers.artifact import resolve_session_file
from qwenpaw_data.host.core.api.workspace_file import open_resolved_workspace_file


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_replaced_directory_cannot_redirect_open(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_text("outside")
    checked = resolve_session_file(root, "reports/report.txt")
    directory.rename(root / "original")
    _symlink_or_skip(directory, outside)
    with pytest.raises(OSError):
        open_resolved_workspace_file(root, checked)


@pytest.mark.skipif(
    os.name == "nt", reason="Windows may not permit renaming an open file"
)
def test_open_handle_stays_bound_when_directory_is_replaced(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.txt").write_text("outside")
    checked = resolve_session_file(root, "reports/report.txt")
    with open_resolved_workspace_file(root, checked) as file:
        directory.rename(root / "original")
        _symlink_or_skip(directory, outside)
        assert file.read() == b"inside"


def test_resolved_in_workspace_symlink_can_be_read(tmp_path):
    root = tmp_path / "workspace"
    directory = root / "reports"
    directory.mkdir(parents=True)
    (directory / "report.txt").write_text("inside")
    _symlink_or_skip(root / "alias", directory)
    checked = resolve_session_file(root, "alias/report.txt")
    with open_resolved_workspace_file(root, checked) as file:
        assert file.read() == b"inside"
