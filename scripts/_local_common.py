"""Cross-platform helpers for QwenPaw Data local lifecycle scripts."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def environment_file(root: Path) -> Path:
    configured = os.getenv("QWENPAW_DATA_ENV_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else root / ".env"


def ensure_environment(root: Path) -> Path:
    env_path = environment_file(root)
    example = root / ".env.example"
    if env_path == root / ".env" and not env_path.exists() and example.exists():
        shutil.copyfile(example, env_path)
        try:
            env_path.chmod(0o600)
        except OSError:
            pass

    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        if "NEO4J_PASSWORD=YOUR_PASSWORD" in text or "NEO4J_PASSWORD=\n" in text:
            password = secrets.token_hex(32)
            text = text.replace(
                "NEO4J_PASSWORD=YOUR_PASSWORD", f"NEO4J_PASSWORD={password}"
            )
            text = text.replace("NEO4J_PASSWORD=\n", f"NEO4J_PASSWORD={password}\n")
            temporary = env_path.with_name(f".{env_path.name}.{os.getpid()}.tmp")
            # Local dev bootstrap: a freshly generated random password is
            # persisted to the user's .env (chmod 0600 before replace below).
            temporary.write_text(text, encoding="utf-8", newline="\n")  # codeql[py/clear-text-storage-sensitive-data]
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, env_path)
            print(f"Generated a random local Neo4j password in {env_path}.")

    load_environment(env_path)
    os.environ["QWENPAW_DATA_ENV_FILE"] = str(env_path)
    return env_path


def load_environment(env_path: Path) -> None:
    """Load the simple KEY=VALUE syntax used by the checked-in env example.

    Existing process variables win. Shell expansion and command substitution are
    deliberately unsupported so the same file behaves consistently on every OS.
    """

    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ValueError(f"Invalid environment key in {env_path}: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_command(command: str) -> str:
    candidate = Path(command).expanduser()
    if candidate.parent != Path(".") and candidate.exists():
        return str(candidate.resolve())
    resolved = shutil.which(command)
    if resolved:
        return resolved
    raise FileNotFoundError(f"Command was not found: {command}")


def venv_executable(venv: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name in {"python", "qwenpaw-data"} else ".cmd"
        return venv / "Scripts" / f"{name}{suffix}"
    return venv / "bin" / name


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    quiet: bool = False,
) -> None:
    command = [os.fspath(item) for item in argv]
    print("+", subprocess.list2cmdline(command))
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        check=False,
        stdout=subprocess.DEVNULL if quiet else None,
    )
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)


def port_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(
    process: subprocess.Popen[bytes], grace: float = 3.0
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            process.terminate()
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace)


def url_host(host: str) -> str:
    value = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"[{value}]" if ":" in value and not value.startswith("[") else value
