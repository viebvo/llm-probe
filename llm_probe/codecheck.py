"""Answer checks that need more than comparing lines: the code of `code_py_*` (run in the sandbox against fixed tests) and
the error summary of `long_ctx_*` (the truth is computed from the same generated log the prompt contains).

Both give a CheckResult like the line checks ("n/3", "n/4"), so they show up in the Check column and the exports. Running
model code is slow (a sandbox process per answer), so results are kept in a small cache file next to the database.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import threading

from .check import CheckItem, CheckResult, _without_reasoning as strip_thinking  # (keeps the code fences of an answer that is all code)
from .config import DATA_DIR

CACHE_FILE = DATA_DIR / "check_cache.jsonl"
CACHE_VERSION = 1  # bump when a check changes: older cached results are then ignored
_cache: dict[str, dict] | None = None
_lock = threading.Lock()


def _cached(key: str, make) -> dict:
    """The result stored under `key`, else make() (and keep it, in memory and in the cache file)."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = {}
            try:
                for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if row.get("v") == CACHE_VERSION:
                            _cache[row["k"]] = row["r"]
                    except (ValueError, KeyError):
                        continue
            except OSError:
                pass
        if key in _cache:
            return _cache[key]
    result = make()
    with _lock:
        _cache[key] = result
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"v": CACHE_VERSION, "k": key, "r": result}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # read-only data folder: the check still works, it just is not remembered
    return result


def _key(kind: str, expected: str, response: str) -> str:
    return hashlib.sha256(f"{kind}\0{expected}\0{response}".encode("utf-8", "replace")).hexdigest()


def _items(rows: list[tuple[str, str, bool]]) -> CheckResult:
    """rows: (label, what is expected, did the answer do it)."""
    items = [CheckItem(expected, expected if ok else None, ok, label) for label, expected, ok in rows]
    return CheckResult(exact=all(i.ok for i in items), correct=sum(i.ok for i in items), total=len(items), items=items)


# ------------------------------------------------------------------------------------------------ code (nginx log parser)

_LINE = ('203.0.113.7 - alice [17/Oct/2026:03:01:17 +0000] "GET /api/v1/items?id=5 HTTP/1.1" 200 1534 '
         '"https://example.com/" "Mozilla/5.0 (X11; Linux x86_64)"')
_FUNCS = r'''
import inspect, dataclasses
fns = [f for n, f in vars(m).items() if inspect.isfunction(f) and f.__module__ == m.__name__ and not n.startswith(("test", "_"))
       and len([p for p in inspect.signature(f).parameters.values() if p.default is p.empty]) == 1]
fns += [getattr(c, n) for c in vars(m).values() if inspect.isclass(c) and not dataclasses.is_dataclass(c)
        for n, f in vars(c).items() if isinstance(f, (staticmethod, classmethod)) and not n.startswith("_")]
assert fns, "no function that takes one log line"
'''
_VALID = _FUNCS + f'''
LINE = {_LINE!r}
ok = None
for f in fns:
    try:
        r = f(LINE)
    except Exception:
        continue
    if not dataclasses.is_dataclass(r) or isinstance(r, type):
        continue
    vals = [str(v) for v in dataclasses.asdict(r).values()]
    if all(any(w == v or w in v for v in vals) for w in ("203.0.113.7", "GET", "/api/v1/items?id=5", "200", "1534", "Mozilla/5.0 (X11; Linux x86_64)")):
        ok = f
        break
assert ok is not None, "no function turned the line into a dataclass with ip, method, path, status, bytes and user agent"
'''
_INVALID = _FUNCS + '''
good = False
for f in fns:
    try:
        r = f("this is not a log line")
    except Exception:
        good = True
        break
    if r is None:
        good = True
        break
assert good, "an invalid line neither raised nor returned None"
'''
_FENCE = re.compile(r"```[ \t]*(?:python|py|python3)?[ \t]*\n(.*?)```", re.S | re.I)


def _module(response: str) -> str:
    """The code of the answer without its tests: every fenced block that parses, minus test functions, pytest imports and
    `if __name__` blocks (the sandbox has no pytest)."""
    answer = strip_thinking(response)
    blocks = _FENCE.findall(answer) or [answer]
    parts = []
    for b in blocks:
        try:
            tree = ast.parse(b)
        except SyntaxError:
            continue  # a directory tree, a shell line, ...
        keep = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                continue
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)) and "pytest" in ast.dump(node):
                continue
            if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
                continue
            keep.append(node)
        tree.body = keep
        if keep:
            parts.append(ast.unparse(tree))
    return "\n\n".join(parts)


def _count_tests(response: str) -> int:
    return len(re.findall(r"^\s*def test_\w*", strip_thinking(response), re.M))


def code_check(response: str, expected: str = "") -> CheckResult:
    """code_py: the function parses a valid combined-format line into a dataclass, an invalid line raises or gives None, and
    the answer has at least three pytest tests. Three items."""
    def run() -> dict:
        from . import sandbox
        code = _module(response)
        if not code.strip():
            return {"valid": "no code", "invalid": "no code"}
        res = sandbox.run_tests(code, {"valid": _VALID, "invalid": _INVALID})
        if "tests" not in res:
            msg = res.get("import_error") or res.get("sandbox_error") or "failed"
            return {"valid": f"error: {str(msg)[:120]}", "invalid": f"error: {str(msg)[:120]}"}
        return res["tests"]

    got = _cached(_key("code", expected, response), run)
    tests = _count_tests(response)
    return _items([
        ("parses a valid line", "a valid line gives a dataclass with ip, method, path, status, bytes, user agent", got.get("valid") == "ok"),
        ("rejects an invalid line", "an invalid line raises or returns None", got.get("invalid") == "ok"),
        ("three tests", "at least 3 pytest test functions", tests >= 3)])


# ------------------------------------------------------------------------------------------------ log summary

_KINDS = {  # error kind -> words in an answer (English, German, Polish); the kind is how the log words it
    "oom": r"\boom\b|out[- ]of[- ]memory|oom[- ]kill|speicher|pami[eę]ci",
    "refused": r"refus|5432|verweiger|abgelehnt|odmow|connection",
    "timeout": r"time-?out|zeit[uü]berschreitung|przekroczen|zeitlimit",
}
_SERVICES = ["nginx", "postgres", "redis", "api-gateway", "worker", "scheduler"]
_NEGATION = re.compile(r"\b(no|none|not|never|without|kein|keine|keinen|nicht|nie|ohne|brak|bez|nie ma)\b", re.I)


def log_truth(log: str) -> dict:
    """{"kinds": {kind: [services with that error]}, "clean": [services without any ERROR]} for the generated log."""
    kinds: dict[str, set[str]] = {k: set() for k in _KINDS}
    seen: set[str] = set()
    for line in log.splitlines():
        m = re.match(r"\S+ (\S+)\s+(\w+)\s+(.*)", line)
        if not m or m.group(2) != "ERROR":
            continue
        seen.add(m.group(1))
        k = "oom" if "OOM" in m.group(3) else "refused" if "refused" in m.group(3) else "timeout"
        kinds[k].add(m.group(1))
    return {"kinds": {k: sorted(v) for k, v in kinds.items()}, "clean": sorted(set(_SERVICES) - seen)}


def _attribute(answer: str) -> tuple[dict[str, set[str]], set[str]]:
    """Which services the answer names under which error kind (a line, or the lines under a heading-like line), and which
    services it blames without a negation."""
    named: dict[str, set[str]] = {k: set() for k in _KINDS}
    blamed: set[str] = set()
    state: set[str] = set()
    header = False
    for raw in answer.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        kinds = {k for k, rx in _KINDS.items() if re.search(rx, low)}
        services = {s for s in _SERVICES if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", low)}
        if kinds:
            state = kinds
            header = line.endswith(":") or line.startswith("#") or (line.startswith("**") and len(services) == 0)
            use = kinds
        elif state and (header or raw.startswith((" ", "\t", "-", "*", "•"))) and not line.startswith("#"):
            use = state
        else:
            use = set()
            if line.startswith("#"):
                state, header = set(), False
        for k in use:
            named[k] |= services
        if "scheduler" in services and (kinds or use) and not _NEGATION.search(low):
            blamed.add("scheduler")
    return named, blamed


def logsvc_check(response: str, expected: str) -> CheckResult:
    """long_ctx: per error kind of the log the answer names the kind and exactly the services that have it, and it does
    not blame the one service without errors. A heuristic on words near each other: one item per kind plus one."""
    truth = json.loads(expected)
    answer = strip_thinking(response)
    named, blamed = _attribute(answer)
    labels = {"oom": "OOM kills", "refused": "connection refused", "timeout": "timeouts"}
    rows: list[tuple[str, str, bool]] = []
    for kind, services in truth["kinds"].items():
        mentioned = bool(re.search(_KINDS[kind], answer.lower()))
        ok = mentioned and set(services) <= named[kind] and not (named[kind] - set(services))
        rows.append((labels[kind], f"{labels[kind]}: {', '.join(services)}", ok))
    for clean in truth["clean"]:
        rows.append((f"{clean} has no errors", f"{clean} is not named as failing", clean not in blamed))
    return _items(rows)
