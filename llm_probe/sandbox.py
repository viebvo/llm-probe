"""Run model-written code against tests in a sandbox.

Only for code from the long-session (deviation) tests. The code runs in a separate, isolated Python process:

* `python -I -S -B`: no environment variables, no user or site packages - only the standard library;
* an empty temporary directory as working directory, removed afterwards; a minimal environment;
* limits for CPU time, memory, file size and open files (Unix), a wall-clock timeout per run and per test;
* Linux with unprivileged namespaces (`unshare -rnm`): no network at all, the whole file system read-only except
  the working directory, and the data directory (config with passwords) and folders such as ~/.ssh hidden;
* everywhere: an audit hook inside the process refuses network access, starting processes, loading native
  libraries and writing or deleting files outside the working directory. It is not a security boundary on its
  own, but covers systems without namespaces (macOS, Windows).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from .config import DATA_DIR

CPU_SECONDS = 20
MEMORY_BYTES = 1024 * 1024 * 1024
WALL_SECONDS = 40
TEST_SECONDS = 5
HIDDEN_HOME_DIRS = (".ssh", ".gnupg", ".config", ".aws", ".kube", ".docker", ".password-store")

# Linux: inside the new user + mount + network namespace. $1 = working directory, $2.. = directories to hide.
_ISOLATE = r'''
wd="$1"; shift
mount --bind "$wd" "$wd" || exit 90
for d in "$@"; do mount -t tmpfs -o ro,size=1k none "$d" 2>/dev/null; done
awk '{print $2}' /proc/self/mounts | sort -u | while read -r m; do
  m=$(printf '%b' "$m")
  [ "$m" = "$wd" ] || mount -o remount,bind,ro "$m" 2>/dev/null
done
cd "$wd" || exit 91
exec "$PYTHON" -I -S -B runner.py "$TEST_SECONDS"
'''

# Runs inside the sandbox: guards the process, imports solution.py and runs every test with a time limit.
_RUNNER = r'''
import json, os, signal, sys
from decimal import Decimal

tests = json.load(open("tests.json", encoding="utf-8"))
TEST = int(sys.argv[1])
WD = os.path.realpath(os.getcwd())

def _inside(path):
    try:
        p = os.path.realpath(os.fsdecode(path) if not isinstance(path, int) else WD)
    except (TypeError, ValueError):
        return False
    return p == WD or p.startswith(WD + os.sep)

_BLOCKED = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
            "os.kill", "ctypes.", "pty.", "webbrowser.", "winreg.", "_winapi.")
_PATHS = ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.link", "os.symlink",
          "os.truncate", "os.utime", "shutil.", "os.replace")

def _guard(event, args):
    if event.startswith(_BLOCKED):
        raise PermissionError(f"not allowed in the sandbox: {event}")
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)) and os.fsdecode(path).startswith("/proc/") and os.fsdecode(path)[6:].split("/")[0].isdigit() \
                and int(os.fsdecode(path)[6:].split("/")[0]) != os.getpid():
            raise PermissionError(f"other processes are not readable in the sandbox: {path}")  # (their command line, environment, memory)
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        if writing and not _inside(path):
            raise PermissionError(f"writing outside the sandbox: {path}")
    elif event.startswith(_PATHS):
        for a in args[:2]:
            if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a):
                raise PermissionError(f"not allowed outside the sandbox: {event} {a}")

sys.addaudithook(_guard)
sys.path.insert(0, WD)

def near(a, b, tol=0.005):
    return abs(float(a) - float(b)) < tol

def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False

def _timeout(*_):
    raise TimeoutError(f"test took longer than {TEST} s")

if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _timeout)
out = {}
try:
    if hasattr(signal, "alarm"):
        signal.alarm(TEST)
    import solution as m
except BaseException as e:
    print("@@RESULT " + json.dumps({"import_error": f"{type(e).__name__}: {str(e)[:300]}"}), flush=True)
    os._exit(0)
for name, src in tests.items():
    ns = {"m": m, "Decimal": Decimal, "near": near, "raises": raises}
    if hasattr(signal, "alarm"):
        signal.alarm(TEST)
    try:
        exec(compile(src, name, "exec"), ns)
        out[name] = "ok"
    except AssertionError as e:
        out[name] = "failed" + (f": {str(e)[:200]}" if str(e) else "")
    except BaseException as e:
        out[name] = f"error: {type(e).__name__}: {str(e)[:200]}"
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)
print("@@RESULT " + json.dumps({"tests": out}), flush=True)
os._exit(0)
'''


def _python() -> str:
    """The base interpreter (not the venv's symlink): -I -S does not use the venv anyway."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


@lru_cache(maxsize=1)
def namespaces() -> bool:
    """Whether unprivileged user, mount and network namespaces work here (unshare -rnm)."""
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "-rnm", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _hidden() -> list[str]:
    """Directories hidden from the code - unless the interpreter itself lives in one of them."""
    py = Path(_python())
    dirs = [DATA_DIR] + [Path.home() / d for d in HIDDEN_HOME_DIRS]
    return [str(d) for d in dirs if d.is_dir() and d not in py.parents]


def description() -> str:
    """How the code is isolated on this machine, for the run details."""
    if namespaces():
        how = "no network, read-only file system except a temporary directory, data directory and ~/.ssh etc. hidden"
    else:
        how = "network, processes and writes outside a temporary directory blocked inside Python (no OS isolation here)"
    limits = "CPU/memory limits, " if os.name == "posix" else ""
    return f"isolated Python (standard library only), {limits}{how}"


def _limits() -> None:  # runs in the child before exec (Unix)
    import resource
    os.setsid()
    for res, value in ((resource.RLIMIT_CPU, CPU_SECONDS), (resource.RLIMIT_AS, MEMORY_BYTES),
                       (resource.RLIMIT_FSIZE, 10 * 1024 * 1024), (resource.RLIMIT_NOFILE, 64)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def run_tests(code: str, tests: dict[str, str]) -> dict:
    """Import `code` as module `solution` (tests refer to it as `m`) and run the tests (name -> Python source
    that raises AssertionError on failure; helpers near() and raises()). Returns
    {"tests": {name: "ok" | "failed..." | "error: ..."}}, {"import_error": ...} or {"sandbox_error": ...}."""
    with tempfile.TemporaryDirectory(prefix="llm-probe-") as wd:
        wd = os.path.realpath(wd)
        for name, text in (("solution.py", code), ("runner.py", _RUNNER), ("tests.json", json.dumps(tests))):
            with open(os.path.join(wd, name), "w", encoding="utf-8") as f:
                f.write(text)
        env = {"PATH": "/usr/bin:/bin", "HOME": wd, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
               "PYTHON": _python(), "TEST_SECONDS": str(TEST_SECONDS)}
        if namespaces():
            cmd = ["unshare", "-rnm", "sh", "-c", _ISOLATE, "isolate", wd, *_hidden()]
        else:
            cmd = [_python(), "-I", "-S", "-B", "runner.py", str(TEST_SECONDS)]
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=WALL_SECONDS,
                               preexec_fn=_limits if os.name == "posix" else None)
        except subprocess.TimeoutExpired:
            return {"sandbox_error": f"no result within {WALL_SECONDS} s"}
        line = next((x for x in reversed(p.stdout.splitlines()) if x.startswith("@@RESULT ")), None)
        if line is None:
            return {"sandbox_error": (p.stderr.strip().splitlines() or [f"exit code {p.returncode}"])[-1][:300]}
        return json.loads(line[9:])
