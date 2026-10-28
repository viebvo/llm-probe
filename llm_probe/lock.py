"""Locks shared by all processes using the same data directory (web UI and command-line runs at the same time).

The locks are operating-system file locks (flock, on Windows msvcrt.locking): the system releases them when the
process ends, also when it crashes or is killed, so a lock can never be left behind. A process that hangs keeps
its lock; the UIs then show who holds it and offer to start anyway.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator

from .config import DATA_DIR

TEST_LOCK = DATA_DIR / "test.lock"  # held while a test or capability check runs; contains who runs it
# Windows locks bytes for real: even reading a locked byte fails for every other handle. The lock therefore sits on a
# byte far behind the text of the file, so the text (who runs the test) stays readable for the other instances.
_LOCK_BYTE = 1 << 30


def _try_lock(f: IO, blocking: bool = False) -> bool:
    if os.name == "nt":
        import msvcrt
        f.seek(_LOCK_BYTE)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def _unlock(f: IO) -> None:
    if os.name == "nt":
        import msvcrt
        f.seek(_LOCK_BYTE)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _open(path: Path) -> IO:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return open(path, "a+", encoding="utf-8")


class TestLock:
    """The lock of the running test. acquire() returns False if another process holds it (see holder())."""

    def __init__(self) -> None:
        self._f: IO | None = None

    def acquire(self, ui: str, endpoint: str, models: list[str], what: str = "test") -> bool:
        f = _open(TEST_LOCK)
        if not _try_lock(f):
            f.close()
            return False
        info = {"pid": os.getpid(), "host": socket.gethostname(), "ui": ui, "what": what, "endpoint": endpoint,
                "models": models, "started": datetime.now().isoformat(timespec="seconds")}
        f.seek(0)
        f.truncate()
        f.write(json.dumps(info))
        f.flush()
        self._f = f
        return True

    def release(self) -> None:
        if self._f is not None:
            self._f.seek(0)
            self._f.truncate()
            _unlock(self._f)
            self._f.close()
            self._f = None


def holder() -> dict | None:
    """Who runs a test right now (pid, host, ui, what, endpoint, models, started), None if nobody."""
    if not TEST_LOCK.exists():
        return None
    with _open(TEST_LOCK) as f:
        if _try_lock(f):  # free: the content (if any) is from a process that has ended
            _unlock(f)
            return None
        f.seek(0)
        try:
            return json.loads(f.read() or "{}") or {"ui": "another process"}
        except (ValueError, OSError):  # OSError: a lock that cannot be read around (should not happen, see _LOCK_BYTE)
            return {"ui": "another process"}


def holder_text(h: dict) -> str:
    """e.g. 'Another LLM Probe instance is already running a test: the web UI on port 8080 (process 4711),
    since 14:05 - ollama-1 · gpt-oss:20b.'"""
    models = ", ".join(h.get("models") or [])
    since = (h.get("started") or "")[11:16]
    proc = f"process {h['pid']}" + (f" on {h['host']}" if h.get("host") and h["host"] != socket.gethostname() else "") \
        if h.get("pid") else ""
    return (f"Another LLM Probe instance is already running a {h.get('what', 'test')}: the {h.get('ui', 'other UI')}"
            + (f" ({proc})" if proc else "") + (f", since {since}" if since else "")
            + (f" - {h.get('endpoint')} · {models}" if h.get("endpoint") else "") + ".")


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Short exclusive lock around reading, changing and writing a shared file (config.json, prompts.json)."""
    with _open(path.with_name(path.name + ".lock")) as f:
        _try_lock(f, blocking=True)
        try:
            yield
        finally:
            _unlock(f)
