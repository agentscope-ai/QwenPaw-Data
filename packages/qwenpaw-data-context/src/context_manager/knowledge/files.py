"""Read and write originals in one user's document directory."""

from pathlib import Path

from ..utils.validation import require_path_component


class DocumentFileStore:
    """Own original-file paths and byte operations."""

    def __init__(
        self, home: Path, *, tenant_id: str, workspace_id: str, user_id: str
    ) -> None:
        home = home.expanduser()
        if not home.is_absolute():
            raise ValueError("QWENPAW_DATA_HOME must be absolute")
        self.home = home.resolve()
        self.target = (
            require_path_component(tenant_id, "tenant_id"),
            require_path_component(workspace_id, "workspace_id"),
            require_path_component(user_id, "user_id"),
        )

    def path(self, filename: str) -> Path:
        path = self.home
        for component in (
            *self.target,
            "kg-docs",
            require_path_component(filename, "filename"),
        ):
            path /= component
            if path.is_symlink():
                raise ValueError("document paths cannot traverse symbolic links")
        return path

    def write(self, filename: str, content: bytes) -> None:
        """Create one original; remove partial writes and preserve existing files."""
        path = self.path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with path.open("xb") as handle:
                created = True
                handle.write(content)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

    def read(self, filename: str) -> bytes:
        return self.path(filename).read_bytes()

    def delete(self, filename: str) -> None:
        self.path(filename).unlink(missing_ok=True)
