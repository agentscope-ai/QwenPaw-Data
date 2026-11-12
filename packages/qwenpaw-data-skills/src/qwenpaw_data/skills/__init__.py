"""Public access to the bundled QwenPaw Data skills, without starting Host or CM."""

from importlib.resources import files
from pathlib import Path

__version__ = "0.4.0"
__all__ = ["bundle_root", "iter_skill_paths", "get_skill_path"]


def bundle_root() -> Path:
    """Locate source-checkout assets or wheel-installed assets."""
    installed = Path(str(files(__package__).joinpath("skills")))
    if installed.is_dir():
        return installed
    legacy = Path(__file__).resolve().parents[2] / "qwenpaw_data_skills" / "skills"
    if legacy.is_dir():
        return legacy
    checkout = Path(__file__).resolve().parents[3] / "skills"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("qwenpaw-data-skills bundle is not installed")


def iter_skill_paths() -> tuple[Path, ...]:
    """Return skill directories in stable category/name order."""
    return tuple(path.parent for path in sorted(bundle_root().rglob("SKILL.md")))


def get_skill_path(name: str) -> Path:
    """Resolve an exact skill name, rejecting paths and ambiguous bundles."""
    import re

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("expected a skill name, not a path")
    matches = [path for path in iter_skill_paths() if path.name == name]
    if not matches:
        raise KeyError(name)
    if len(matches) != 1:
        raise ValueError(f"ambiguous bundled skill: {name}")
    return matches[0]
