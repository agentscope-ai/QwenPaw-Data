"""Release wheels/sdists must carry every prebuilt browser application."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ConsoleBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        root = Path(self.root) / "src/qwenpaw_data/console/static"
        for app in ("chat", "chat-embed", "databridge"):
            if not (root / app / "index.html").is_file():
                raise RuntimeError(
                    "Console assets are missing. From the QwenPaw-Data checkout, "
                    "run: python scripts/build_console.py"
                )
