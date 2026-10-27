"""SQLite storage for runs, individual results and resource samples."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from statistics import mean, median

from . import placement
from .client import billions
from .check import CheckItem, CheckResult, call_label, faithfulness, reasoning_share, refusal_verdict  # noqa: F401 (call_label: re-exported)
from .config import DATA_DIR, DB_FILE, clean_currency
from .privacy import mask_base_url
from .prompts import ADAPTIVE_IDS, BUILTIN_PROMPTS, SWEEP_PASS, Prompt, all_prompts

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL,            -- running | done | aborted | failed | interrupted
    endpoint TEXT NOT NULL,
    kind TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    settings TEXT NOT NULL,          -- JSON
    gpu_info TEXT,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,             -- single | load
    prompt_id TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    rep INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    ttft REAL, total REAL,
    ttfa REAL,                       -- time to the first answer token (after reasoning)
    prompt_tokens INTEGER, completion_tokens INTEGER,
    decode_tps REAL, e2e_tps REAL,
    wall REAL,                       -- load only: wall time of the level
    text TEXT, error TEXT,
    tool_calls TEXT,                 -- single only: JSON list of {name, arguments}
    extra TEXT,                      -- session only: JSON result of the step (deviation.evaluate_step)
    tokens_estimated INTEGER,        -- 1 = output tokens counted from stream chunks (server sent no usage)
    cut TEXT                         -- the response was cut off: 'tokens' = at max_tokens, 'time' = at the time limit
);
CREATE TABLE IF NOT EXISTS samples (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    t REAL NOT NULL,                 -- seconds since run start
    phase TEXT,
    cpu_pct REAL, mem_used_mb REAL, mem_total_mb REAL,
    gpus TEXT                        -- JSON list
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
"""


def mark_interrupted(db: sqlite3.Connection) -> int:
    """Runs still marked 'running' although no process runs a test (the program or the computer was stopped in the middle):
    they become 'interrupted' with the reason stored; what came in stays valid. The caller makes sure nobody holds the test lock."""
    n = 0
    for r in db.execute("SELECT id, settings FROM runs WHERE status = 'running'").fetchall():
        settings = json.loads(r["settings"])
        settings.setdefault("error", "The test was interrupted: the program or the computer was stopped while it ran.")
        db.execute("UPDATE runs SET status = 'interrupted', settings = ? WHERE id = ? AND status = 'running'", (json.dumps(settings), r["id"]))
        n += 1
    if n:
        db.commit()
    return n


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # the web UI and command-line runs may use the database at the same time: WAL lets readers and the writer work in parallel,
    # and a busy database is waited for instead of failing at once
    db = sqlite3.connect(DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    # columns added later: databases created before get them, empty
    have = {r["name"] for r in db.execute("PRAGMA table_info(results)")}
    for col, decl in (("cut", "TEXT"),):
        if col not in have:
            db.execute(f"ALTER TABLE results ADD COLUMN {col} {decl}")
    return db


def runs_version(db: sqlite3.Connection) -> str:
    """Changes whenever a run is added, finished, deleted or gets a note - and while a run is running, with every
    stored result (the newest result id) - to refresh run lists and open views cheaply."""
    r = db.execute("SELECT count(*), max(id), max(finished), sum(length(note)), group_concat(status) FROM runs").fetchone()
    last = db.execute("SELECT max(id) FROM results").fetchone()[0]
    return "|".join(str(x) for x in (*r, last))


def list_runs(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()


def get_run(db: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def delete_run(db: sqlite3.Connection, run_id: int) -> None:
    db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    db.commit()
    _RESOURCE_CACHE.pop(run_id, None)  # an id may be reused by a later run


def run_label(r: sqlite3.Row) -> str:
    return f"#{r['id']} {r['endpoint']} · {r['model']} · {r['started'][:16]} ({r['status']})"


# ---------------------------------------------------------------- Evaluation

def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _avg(values: list) -> float | None:
    v = [x for x in values if x is not None]
    return mean(v) if v else None


@dataclass
class PromptStats:
    prompt_id: str
    n: int
    errors: int
    ttft: float | None  # Median
    ttfa: float | None  # time to the answer (after reasoning)
    decode_tps: float | None
    e2e_tps: float | None
    total: float | None
    prompt_tokens: float | None
    completion_tokens: float | None
    check_n: int = 0  # successful responses checked against an expected template
    check_exact: int = 0  # ... of which matched the template exactly
    check_score: float | None = None  # mean share of correct lines
    cut_tokens: int = 0  # successful responses cut off at max_tokens
    cut_time: int = 0  # ... at the time limit


@dataclass
class LoadStats:
    concurrency: int
    n: int
    errors: int
    throughput: float | None  # sum of completion tokens / wall time
    req_per_s: float | None
    ttft_avg: float | None
    ttft_p95: float | None
    ttfa_avg: float | None
    latency_avg: float | None
    latency_p95: float | None
    tps_per_req: float | None


@dataclass
class ResourceStats:
    phase: str
    n: int
    gpu_util_avg: float | None
    gpu_util_max: float | None
    vram_max_mb: float | None
    vram_total_mb: float | None
    power_avg: float | None
    power_max: float | None
    temp_max: float | None
    cpu_avg: float | None
    cpu_max: float | None
    mem_max_mb: float | None
    gpu_util_each: list[float] = field(default_factory=list)  # average utilization per GPU, in nvidia-smi order
    imbalanced: bool = False  # several GPUs, but some sit idle while another works (display only, no other result uses it)


def _share(row: sqlite3.Row) -> float | None:
    """Reasoning share of a stored response (None = no text stored)."""
    return None if row["text"] is None else reasoning_share(row["text"])


def _calls(row: sqlite3.Row) -> list[dict]:
    return json.loads(row["tool_calls"]) if row["tool_calls"] else []


# columns every query that feeds evaluate() must select (see _col: a missing one silently disables the detection)
EVAL_COLS = "prompt_id, rep, text, tool_calls, cut, ok, prompt_tokens, tokens_estimated, extra"
# a text-only prompt of at least this many characters whose prompt token count is below chars / this was cut by the
# server (Ollama drops the start of a prompt that exceeds the per-request context): the log text needs ~2.3 chars per
# token, normal English prose stays below ~5; the hard log triage of #67-#76 had 6.8 (56,045 chars in 8,194 tokens)
TRUNC_CHARS_PER_TOKEN = 5.5
TRUNC_MIN_CHARS = 6000


def _col(row: sqlite3.Row, name: str):
    return row[name] if name in row.keys() else None


def row_extra(row: sqlite3.Row) -> dict:
    """The per-response JSON a run stores in results.extra: for a prompt generated at run time its expected answer,
    size and the context it was made for (bench.BenchmarkRunner._prepare_adaptive)."""
    raw = _col(row, "extra")
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def truncated(p: Prompt | None, row: sqlite3.Row) -> bool:
    """Did the server cut this response's prompt? Judged when a result is read (so older runs are covered too):
    skipped for prompts with images, estimated token counts, short prompts and the overflow test (which is meant to
    be cut - see overflowed)."""
    if p is None or p.images or p.adaptive == "overflow" or _col(row, "tokens_estimated"):
        return False
    pt, ex = _col(row, "prompt_tokens"), row_extra(row)
    chars = ex.get("chars") or (len(p.text) if not p.adaptive else 0)
    if not pt or chars < TRUNC_MIN_CHARS:
        return False
    return chars / pt > TRUNC_CHARS_PER_TOKEN or bool(ex.get("est_tokens") and pt < 0.8 * ex["est_tokens"])


def overflowed(row: sqlite3.Row) -> bool:
    """Did the overflow test overflow? The server rejected the request, or processed clearly fewer prompt tokens than
    the prompt has."""
    if (_col(row, "text") or "").startswith("[rejected]"):
        return True
    pt, est = _col(row, "prompt_tokens"), row_extra(row).get("est_tokens")
    return bool(pt and est and pt < 0.9 * est)


def evaluate(p: Prompt | None, row: sqlite3.Row) -> CheckResult | None:
    """Check of a stored response (None = prompt unknown or without expected answer), with whether it was cut off.
    A prompt generated at run time is checked against the answer stored with the response. A response whose prompt the
    server truncated is not counted (undecided, cut 'context'); so is an overflow test that did not overflow."""
    c = p.evaluate(row["text"] or "", _calls(row), row_extra(row).get("expected"), row_extra(row).get("escape", "")) if p else None
    if c is None:
        return None
    if p.check == "overflow":
        if not overflowed(row) and not c.undecided:
            item = CheckItem("reports that it cannot answer (unsolvable / server error)", "the whole prompt was processed - no overflow happened", False, "overflow")
            c = replace(c, undecided=True, correct=0, items=[item], unsolvable=0)
    elif truncated(p, row):
        return replace(c, cut="context", undecided=True)
    return replace(c, cut=row["cut"]) if row["cut"] else c  # the check itself is cached


def single_stats(db: sqlite3.Connection, run_id: int) -> list[PromptStats]:
    rows = db.execute("SELECT * FROM results WHERE run_id = ? AND phase = 'single' ORDER BY id", (run_id,)).fetchall()
    by_prompt: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_prompt.setdefault(r["prompt_id"], []).append(r)
    prompts = {p.id: p for p in all_prompts() if p.expected}
    out = []
    for pid, rs in by_prompt.items():
        ok = [r for r in rs if r["ok"]]
        # c.total == 0 means an all-provenance ("smoke") prompt: it has no normal score, only a smoke tally, so it
        # does not belong in the legacy check figures (otherwise it would show as 0 % and drag the Check column)
        checks = [c for c in (evaluate(prompts[pid], r) for r in ok) if not c.undecided and c.total] if pid in prompts else []

        def med(col: str) -> float | None:
            v = [r[col] for r in ok if r[col] is not None]
            return median(v) if v else None

        out.append(PromptStats(pid, len(rs), len(rs) - len(ok), med("ttft"), med("ttfa"), med("decode_tps"), med("e2e_tps"),
                               med("total"), med("prompt_tokens"), med("completion_tokens"), len(checks),
                               sum(c.exact for c in checks), _avg([c.score for c in checks]),
                               sum(r["cut"] == "tokens" for r in ok), sum(r["cut"] == "time" for r in ok)))
    return out


def cut_label(db: sqlite3.Connection, run_id: int, settings: dict) -> str:
    """e.g. '12 of 114 responses at max_tokens (1024) - raise it; 1 at the time limit (300 s)'; '' = none cut."""
    r = db.execute("SELECT count(*), sum(cut = 'tokens'), sum(cut = 'time') FROM results WHERE run_id = ?"
                   " AND phase IN ('single', 'session') AND ok = 1", (run_id,)).fetchone()
    n, tokens, slow = r[0], r[1] or 0, r[2] or 0
    parts = []
    if tokens:
        parts.append(f"{tokens} of {n} responses at max_tokens ({settings.get('max_tokens', '?')}) - raise it if "
                     "they matter")
    if slow:
        parts.append(f"{slow} of {n} at the time limit ({settings.get('request_timeout', '?'):g} s)"
                     if isinstance(settings.get("request_timeout"), (int, float)) else f"{slow} of {n} at the time limit")
    prompts = {p.id: p for p in all_prompts()}
    cut_ctx = {}
    for row in db.execute(f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'single' AND ok = 1", (run_id,)):
        if truncated(prompts.get(row["prompt_id"]), row):
            cut_ctx[row["prompt_id"]] = cut_ctx.get(row["prompt_id"], 0) + 1
    if cut_ctx:
        parts.append(f"{sum(cut_ctx.values())} responses had their prompt cut by the server ("
                     + ", ".join(prompts[p].name if p in prompts else p for p in cut_ctx)
                     + ") - the context per request is too small for them; not counted")
    return "; ".join(parts)


def load_stats(db: sqlite3.Connection, run_id: int) -> list[LoadStats]:
    rows = db.execute("SELECT * FROM results WHERE run_id = ? AND phase = 'load' ORDER BY id", (run_id,)).fetchall()
    by_c: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_c.setdefault(r["concurrency"], []).append(r)
    out = []
    for c in sorted(by_c):
        rs = by_c[c]
        ok = [r for r in rs if r["ok"]]
        wall = max((r["wall"] or 0) for r in rs)
        tokens = sum(r["completion_tokens"] or 0 for r in ok)
        ttfts = [r["ttft"] for r in ok if r["ttft"] is not None]
        lats = [r["total"] for r in ok if r["total"] is not None]
        out.append(LoadStats(
            c, len(rs), len(rs) - len(ok),
            tokens / wall if wall else None,
            len(ok) / wall if wall else None,
            _avg(ttfts), _pct(ttfts, 0.95), _avg([r["ttfa"] for r in ok]), _avg(lats), _pct(lats, 0.95),
            _avg([r["decode_tps"] for r in ok]),
        ))
    return out


# a GPU counts as idle below this average utilization (%), another as working above that one: "imbalanced" is
# "some of the GPUs sat idle while another one worked" - typically a model that fits on one GPU of a multi-GPU server
GPU_IDLE_PCT, GPU_BUSY_PCT = 5.0, 20.0


def gpus_imbalanced(each: list[float]) -> bool:
    return len(each) >= 2 and max(each) >= GPU_BUSY_PCT and min(each) < GPU_IDLE_PCT


_RESOURCE_CACHE: dict[int, list[ResourceStats]] = {}  # finished runs only: their samples never change


def resource_stats(db: sqlite3.Connection, run_id: int) -> list[ResourceStats]:
    """Resource figures per phase. Parsing thousands of GPU samples takes ~100 ms per run, so the result of a
    finished run is memoized (a run's details and every comparison of it ask for it again)."""
    if run_id in _RESOURCE_CACHE:
        return _RESOURCE_CACHE[run_id]
    out = _resource_stats(db, run_id)
    status = db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    if status and status["status"] != "running":
        _RESOURCE_CACHE[run_id] = out
    return out


def _resource_stats(db: sqlite3.Connection, run_id: int) -> list[ResourceStats]:
    rows = db.execute("SELECT * FROM samples WHERE run_id = ? ORDER BY t", (run_id,)).fetchall()
    out = []
    for phase in ("single", "load", None):
        rs = [r for r in rows if phase is None or r["phase"] == phase]
        if not rs:
            continue
        util, vram, vtot, power, temp = [], [], [], [], []
        each: dict[int, list[float]] = {}  # utilization samples per GPU index
        for r in rs:
            gpus = json.loads(r["gpus"] or "[]")
            if gpus:
                # multiple GPUs: utilization averaged, VRAM/power summed
                for i, g in enumerate(gpus):
                    each.setdefault(i, []).append(g["util"])
                util.append(sum(g["util"] for g in gpus) / len(gpus))  # not statistics.mean: ~10x slower on thousands of samples
                vram.append(sum(g["mem_used"] for g in gpus))
                vtot.append(sum(g["mem_total"] for g in gpus))
                pw = [g["power"] for g in gpus if g["power"] is not None]
                if pw:
                    power.append(sum(pw))
                tp = [g["temp"] for g in gpus if g["temp"] is not None]
                if tp:
                    temp.append(max(tp))
        per_gpu = [sum(v) / len(v) for _, v in sorted(each.items())]
        cpu = [r["cpu_pct"] for r in rs if r["cpu_pct"] is not None]
        mem = [r["mem_used_mb"] for r in rs if r["mem_used_mb"] is not None]
        out.append(ResourceStats(
            phase or "total", len(rs), _avg(util), max(util, default=None), max(vram, default=None),
            max(vtot, default=None), _avg(power), max(power, default=None), max(temp, default=None),
            _avg(cpu), max(cpu, default=None), max(mem, default=None),
            per_gpu, gpus_imbalanced(per_gpu),
        ))
    return out


def single_reps(db: sqlite3.Connection, run_id: int) -> dict[str, list[dict]]:
    """Per prompt of the single phase: the timing of every successful repetition (ttft, ttfa, total in seconds, and
    how it was cut off). The per-prompt stats are medians over these; "where the time goes" draws one thin bar per
    repetition. ttfa None = no answer token ever arrived (the client sets it at the first one, so a response that was
    cut off while still reasoning - the reasoning stream is stored wrapped in a closed <think> block either way - has
    none): the bar draws that time hatched as 'no answer' instead of as an answer."""
    rows = db.execute("SELECT prompt_id, rep, ttft, ttfa, total, cut FROM results WHERE run_id = ? AND phase = 'single' "
                      "AND ok = 1 AND total IS NOT NULL ORDER BY rep, id", (run_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["prompt_id"], []).append({"rep": r["rep"], "ttft": r["ttft"], "ttfa": r["ttfa"], "total": r["total"],
                                                   "cut": r["cut"] or ""})
    return out


def run_summary(db: sqlite3.Connection, run_id: int) -> tuple[float | None, ...]:
    """(avg decode tok/s, avg TTFT, avg TTFA, peak load throughput, avg check score) for run lists."""
    single = [s for s in single_stats(db, run_id) if s.prompt_id not in ADAPTIVE_IDS]  # their size follows the context
    peak = max((l.throughput for l in load_stats(db, run_id) if l.throughput), default=None)
    return (_avg([s.decode_tps for s in single]), _avg([s.ttft for s in single]), _avg([s.ttfa for s in single]),
            peak, _avg([s.check_score for s in single]))


# The results list recomputes per-run aggregates (summary, reasoning share, contents) on every poll, which parses
# every stored response - hundreds of milliseconds for a full database. A finished run's results never change, so
# memoize these aggregates per run, keyed by what could change them (status + finished time). Running runs are not
# cached. The query cost itself is negligible; the parsing is what this saves.
_LIST_CACHE: dict[int, tuple[tuple, dict]] = {}


def invalidate_list_cache() -> None:
    """Drop the memoized list aggregates - call when something that is recomputed at read time changes, e.g. a
    custom prompt's expected answer or check (which changes a run's check score without changing the run itself)."""
    _LIST_CACHE.clear()


def run_list_stats(db: sqlite3.Connection, run: sqlite3.Row) -> dict:
    """{summary, thinking, contents} for the results list, cached for finished (immutable) runs."""
    key = (run["status"], run["finished"])
    hit = _LIST_CACHE.get(run["id"])
    if hit and hit[0] == key:
        return hit[1]
    stats = {"summary": run_summary(db, run["id"]), "thinking": thinking_stats(db, run["id"]),
             "contents": run_contents(db, run["id"]), "scores": run_scores(db, run)}
    if run["status"] != "running":  # a running run's data still grows
        if len(_LIST_CACHE) > 1024:
            _LIST_CACHE.clear()
        _LIST_CACHE[run["id"]] = (key, stats)
    return stats


def thinking_stats(db: sqlite3.Connection, run_id: int) -> dict | None:
    """Whether the model reasoned, from the stored responses of the single phase (None without any).

    Keys: n (responses), thought (... of which contain reasoning), share (mean share of the output that is
    reasoning, by characters), tokens (estimated reasoning tokens per response).
    """
    rows = [r for r in db.execute("SELECT text, extra, completion_tokens FROM results WHERE run_id = ?"
                                  " AND phase = 'single' AND ok = 1", (run_id,)).fetchall() if _share(r) is not None]
    if not rows:
        return None
    shares = [_share(r) for r in rows]
    return {"n": len(rows), "thought": sum(x > 0 for x in shares), "share": mean(shares),
            "tokens": _avg([r["completion_tokens"] * x for r, x in zip(rows, shares) if r["completion_tokens"]])}


def thinking_warn(settings: dict, th: dict | None) -> bool:
    """Reasoning was switched off but the model reasoned anyway."""
    return settings.get("thinking") == "off" and bool(th and th["thought"])


def thinking_label(settings: dict, th: dict | None, short: bool = False) -> str:
    """e.g. 'yes · 64 % of output (~310 tok/response) - requested: off'; short: what was asked and what came of it,
    'model default -> reasoned 64 %', 'off -> no reasoning'."""
    mode = settings.get("thinking") or ""
    if th is None:
        result = "-"
    elif not th["thought"]:
        result = "no reasoning" if short else "no"
    else:
        part = "" if th["thought"] == th["n"] else f" {th['thought']}/{th['n']}"
        if short:
            result = f"reasoned{part} {th['share'] * 100:.0f} %"
        else:
            result = (f"yes{part and ' (' + part.strip() + ' responses)'} · {th['share'] * 100:.0f} % of output"
                      + (f" (~{th['tokens']:.0f} tok/response)" if th["tokens"] else ""))
    if short:
        return "-" if th is None and not mode else f"{mode or 'model default'} -> {result}"
    notes = [f"requested: {mode}"] if mode else []
    if settings.get("thinking_note"):
        notes.append(settings["thinking_note"])
    return result + (f" - {', '.join(notes)}" if notes else "")


def context_label(settings: dict) -> str:
    """Context length a run was served with, e.g. '16384 (num_ctx 16384, model max 262144)'."""
    ctx = settings.get("context")
    if not ctx:
        return "-"  # failed before the warmup
    extra = []
    if ctx.get("num_ctx"):
        extra.append(f"num_ctx {ctx['num_ctx']}")
    if ctx.get("model_max"):
        extra.append(f"model max {ctx['model_max']}")
    eff = (ctx.get("effective") or {}).get("tokens")
    if eff and eff != ctx.get("context"):
        extra.append(f"effective per request {eff}")
    return (str(ctx["context"]) if ctx.get("context") else "unknown") + (f" ({', '.join(extra)})" if extra else "")


def kv_peak_label(settings: dict) -> str:
    """vLLM: peak KV cache usage per phase, e.g. 'single 4 % · load 87 %'."""
    peak = settings.get("kv_peak") or {}
    return " · ".join(f"{ph} {peak[ph] * 100:.0f} %" for ph in ("single", "load") if ph in peak)


def capability_label(settings: dict) -> str:
    """e.g. 'completion, vision, tools (ollama /api/show)' or 'tools: probe refused - 5 prompts skipped (no tools)'."""
    caps = settings.get("capabilities")
    if not caps:
        return "-"  # failed before the warmup
    parts = []
    if caps.get("declared") is not None:
        parts.append(", ".join(caps["declared"]) + (f" ({caps['source']})" if caps.get("source") else ""))
    parts += [f"{cap}: probe {result}" for cap, result in caps.get("probed", {}).items()]
    text = "; ".join(parts) or "not declared by the server"
    if skipped := caps.get("skipped"):
        reasons = sorted(set(skipped.values()))
        text += f" - {len(skipped)} prompt(s) skipped ({', '.join(reasons)})"
    return text


QUANT_SOURCES = {"declared": "declared by the server", "command line": "from the server's command line",
                 "guessed": "guessed from the model name"}


def model_info_label(settings: dict) -> str:
    """Quantization and parameter size of the tested model with where they come from, e.g.
    'Q4_K_M (declared by the server) · 8.8B'; '-' for runs made before they were stored."""
    info = settings.get("model_info") or {}
    parts = []
    if info.get("quant"):
        parts.append(f"{info['quant']} ({QUANT_SOURCES.get(info.get('quant_source'), 'source unknown')})")
    if info.get("size"):
        parts.append(billions(info["size"]) + (" (guessed)" if info.get("size_source") == "guessed" else ""))
    return " · ".join(parts) or "-"


def quant_mismatch(settings: list[dict], labels: list[str]) -> str:
    """Warning if the compared runs used different quantizations (speed and quality are not comparable then)."""
    quants = [((st.get("model_info") or {}).get("quant") or "") for st in settings]
    known = [(lab, q) for lab, q in zip(labels, quants) if q]
    return ("Warning: quantization differs (" + ", ".join(f"{lab} {q}" for lab, q in known) + ")."
            if len({q.upper() for _, q in known}) > 1 else "")


def server_lines(settings: dict, th: dict | None = None, cut: str = "") -> list[tuple[str, str, bool]]:
    """(label, text, highlight) lines about how the model was served, for run details; th: thinking_stats(),
    cut: cut_label()."""
    pl = settings.get("placement")
    lines = [("Thinking", thinking_label(settings, th), thinking_warn(settings, th)),
             ("Capabilities", capability_label(settings), bool((settings.get("capabilities") or {}).get("skipped"))),
             ("Context", context_label(settings), False),
             ("Quantization", model_info_label(settings), False),
             ("Placement", placement.label(pl), placement.needs_attention(pl)),
             ("Server parameters", placement.params_label(pl), False)]
    if kv := kv_peak_label(settings):
        lines.append(("Peak KV cache usage", kv, False))
    if settings.get("sandbox"):  # long sessions: how the model's code was isolated
        lines.append(("Sandbox", settings["sandbox"], False))
    if cut:
        lines.append(("Cut off", cut, True))
    return lines


def compare_warnings(settings: list[dict], thinking: list[dict | None], labels: list[str]) -> list[str]:
    """Differences in how the compared runs were served (thinking, context, placement, server parameters).

    settings/thinking: the runs' settings and thinking_stats(); labels: their letters, the first is the baseline.
    """
    out = [w for w in [thinking_mismatch(thinking, labels), context_mismatch(settings, labels),
                       quant_mismatch(settings, labels)] if w]
    base = settings[0].get("placement")
    for st, name in zip(settings[1:], labels[1:]):
        out += ["Warning: " + w + "." for w in placement.mismatch(base, st.get("placement"), (labels[0], name))]
    return out


def prompt_hash_check(settings: list[dict], labels: list[str], name=lambda p: p) -> tuple[list[str], list[str]]:
    """(errors, notes): errors = prompts the compared runs did not ask identically (hashes of the same id differ,
    shown red); notes = runs without prompt hashes (made before they were stored), which cannot be checked."""
    hashes = [st.get("prompt_hashes") for st in settings]
    missing = [lab for h, lab in zip(hashes, labels) if h is None]
    ids = sorted({p for h in hashes if h for p in h})
    bad = []
    for p in ids:
        seen = [(lab, h[p]) for h, lab in zip(hashes, labels) if h and p in h]
        if len({v for _, v in seen}) > 1:
            groups: dict[str, list[str]] = {}
            for lab, v in seen:
                groups.setdefault(v, []).append(lab)
            bad.append(f"{name(p)} ({' vs '.join('+'.join(g) for g in groups.values())})")
    errors = []
    if bad:
        errors.append("Prompt hashes do not match: " + "; ".join(bad[:12]) + (f"; +{len(bad) - 12} more" if len(bad) > 12 else "") +
                      ". These prompts were not asked identically in the compared runs (different prompt wording, image or tools), so their results are not comparable.")
    notes = [f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no prompt hashes recorded: the prompts of {'that run' if len(missing) == 1 else 'those runs'} could not be checked."] if missing else []
    return errors, notes


def thinking_mismatch(thinking: list[dict | None], labels: list[str]) -> str:
    """Warning if only some runs reasoned: TTFT and durations then measure different things."""
    known = [(th, name) for th, name in zip(thinking, labels) if th is not None]
    who = [name for th, name in known if th["thought"]]
    if not who or len(who) == len(known):
        return ""
    return (f"Warning: only {', '.join(who)} reasoned - TTFT (first reasoning token) and durations are not "
            "comparable; TTFA (time to the answer) and decode tok/s are.")


def context_mismatch(settings: list[dict], labels: list[str]) -> str:
    """Warning if the runs were served with different context lengths (affects VRAM, speed and long prompts)."""
    ctx = [((st.get("context") or {}).get("context"), name) for st, name in zip(settings, labels)]
    ctx = [(c, name) for c, name in ctx if c]
    if len({c for c, _ in ctx}) > 1:
        return (f"Warning: different context lengths ({', '.join(f'{name} {c}' for c, name in ctx)}) - "
                "VRAM use and speed are not directly comparable.")
    return ""


def ttfa_note(ttft: float | None, ttfa: float | None) -> str:
    """Log suffix for one response: when the answer started, if reasoning came first (Rich markup)."""
    if ttfa is None:
        return " · [yellow]no answer[/]"
    return f" · answer after {ttfa:.2f} s" if ttft is not None and ttfa - ttft >= 0.05 else ""


def check_label(s: PromptStats) -> str:
    """e.g. '87 %': how much of the checked content is correct, averaged over the repetitions. Empty when not checked."""
    if not s.check_n:
        return ""
    return f"{s.check_score * 100:.0f} %"


# (name, PromptStats attribute, higher is better, digits, unit); averaged over the prompts both runs have
KPIS = [("Decode tok/s", "decode_tps", True, 1, ""), ("TTFA", "ttfa", False, 2, " s"), ("TTFT", "ttft", False, 2, " s")]


def common_prompts(stats: list[dict[str, PromptStats]]) -> list[str]:
    """Prompts all runs have (in the order of the first run)."""
    return [p for p in stats[0] if all(p in s for s in stats[1:])] if stats else []


def prompt_token_mismatch(stats: list[dict[str, PromptStats]], labels: list[str], name=lambda p: p) -> str:
    """Warning for prompts whose prompt tokens differ by >20 % from the baseline (other tokenizer, truncation)."""
    base, out = stats[0], []
    for p in common_prompts(stats):
        a = base[p].prompt_tokens
        off = [lab for st, lab in zip(stats[1:], labels[1:])
               if a and st[p].prompt_tokens and abs(a - st[p].prompt_tokens) / max(a, st[p].prompt_tokens) > 0.2]
        if off:
            out.append(f"{name(p)} ({', '.join(off)})")
    if not out:
        return ""
    return (f"Warning: prompt tokens differ by >20 % from {labels[0]} for " + ", ".join(out) + ". Different "
            "model/tokenizer, or Ollama truncates the context (num_ctx / OLLAMA_CONTEXT_LENGTH too small).")


def run_seconds(run: sqlite3.Row) -> float | None:
    """Wall-clock time of a finished run in seconds (None while it runs or if the timestamps are unusable)."""
    if run is None or run["status"] == "running" or not run["finished"]:
        return None
    try:
        sec = (datetime.fromisoformat(run["finished"]) - datetime.fromisoformat(run["started"])).total_seconds()
    except (TypeError, ValueError):
        return None
    return sec if sec >= 0 else None


def run_timing(db: sqlite3.Connection, run: sqlite3.Row) -> dict | None:
    """Where the time of a finished run went, for the run-time estimate of the Test tab: seconds per prompt (median over
    repetitions) of the single phase, the sweep and the long sessions, wall seconds per concurrency level of the load
    phase and the rest (warmup, capability check, placement ...) as `overhead`. None if the run is not usable."""
    total = run_seconds(run)
    if total is None or run["status"] != "done":
        return None
    st = json.loads(run["settings"])
    per: dict[str, dict[str, list[float]]] = {"single": {}, "sweep": {}, "session": {}}
    load: dict[int, float] = {}
    for r in db.execute("SELECT phase, prompt_id, concurrency, total, wall FROM results WHERE run_id = ?", (run["id"],)):
        if r["phase"] == "load":
            if r["wall"] is not None:
                load[r["concurrency"]] = r["wall"]
        elif r["total"] is not None and r["phase"] in per:
            per[r["phase"]].setdefault(r["prompt_id"], []).append(r["total"])
    one = {"single": {pid: round(median(v), 2) for pid, v in per["single"].items()}}
    for ph in ("sweep", "session"):  # all steps count: the sizes of the sweep, the steps of a session
        one[ph] = {pid: round(sum(v), 1) for pid, v in per[ph].items()}
    reps = max(1, int(st.get("repetitions") or 1))
    counted = sum(sum(v) for d in per.values() for v in d.values()) + sum(load.values())
    return {"id": run["id"], "thinking": st.get("thinking") or "", "max_tokens": st.get("max_tokens"),
            "reps": reps, "rps": st.get("requests_per_level"),
            "single": one["single"], "sweep": one["sweep"], "session": one["session"],
            "load": {str(c): round(w, 1) for c, w in load.items()},
            "overhead": round(max(0.0, total - counted), 1), "total": round(total, 1)}


def timing_profile(db: sqlite3.Connection, endpoint: str, budget: float = 3.0, per_model: int = 3) -> dict:
    """Timing of the newest finished runs of every model of an endpoint ({model: [run_timing, ...]}), newest first. Stops
    after `budget` seconds: models not reached are missing (unknown), `partial` says so and `runs` counts the runs read."""
    t0 = time.monotonic()
    models: dict[str, list[dict]] = {}
    read, partial = 0, False
    for run in db.execute("SELECT * FROM runs WHERE endpoint = ? AND status = 'done' ORDER BY id DESC", (endpoint,)):
        if len(models.get(run["model"], [])) >= per_model:
            continue
        if time.monotonic() - t0 > budget:
            partial = True
            break
        if t := run_timing(db, run):
            models.setdefault(run["model"], []).append(t)
            read += 1
    return {"models": models, "runs": read, "partial": partial}


def run_scope(db: sqlite3.Connection, run: sqlite3.Row) -> dict:
    """What a run tested: the prompts per phase that have a stored result plus the settings that size the phases."""
    phases: dict[str, set[str]] = {}
    for r in db.execute("SELECT DISTINCT phase, prompt_id FROM results WHERE run_id = ?", (run["id"],)):
        phases.setdefault(r["phase"], set()).add(r["prompt_id"])
    st = json.loads(run["settings"])
    return {"phases": phases, "settings": {k: st.get(k) for k in ("repetitions", "concurrency_levels", "requests_per_level")}}


def scope_warning(scopes: list[dict], labels: list[str]) -> str:
    """Warning if the compared runs did not run the same tests (other prompts, phases, repetitions or load levels): their
    total runtimes are then not comparable. Empty if the test sets are the same."""
    keys = lambda sc: {(ph, p) for ph, ps in sc["phases"].items() for p in ps}
    base = scopes[0]
    differs = [lab for sc, lab in zip(scopes[1:], labels[1:]) if keys(sc) != keys(base) or sc["settings"] != base["settings"]]
    if not differs:
        return ""
    count = lambda sc: ", ".join(f"{ph} {len(ps)}" for ph, ps in sorted(sc["phases"].items())) or "nothing"
    return ("Test sets differ? " + "; ".join(f"{lab}: {count(sc)}" for sc, lab in zip(scopes, labels)) +
            " (prompts with a result per phase, repetitions and load levels also compared). The runs did not run the "
            "same tests, so their runtimes are not comparable.")


def compare_kpis(db: sqlite3.Connection, run_ids: list[int]) -> list[dict]:
    """Key figures of the compared runs (values in run order), averaged over the prompts all of them have."""
    stats = [{s.prompt_id: s for s in single_stats(db, r)} for r in run_ids]
    common = [p for p in common_prompts(stats) if p not in ADAPTIVE_IDS]  # generated prompts differ in size per run
    out = [{"name": name, "values": [_avg([getattr(st[p], attr) for p in common]) for st in stats],
            "higher_better": hb, "digits": digits, "unit": unit}
           for name, attr, hb, digits, unit in KPIS]
    peaks = [max((l.throughput for l in load_stats(db, r) if l.throughput), default=None) for r in run_ids]
    out.append({"name": "Max throughput", "values": peaks, "higher_better": True, "digits": 1, "unit": " tok/s"})
    runs = [get_run(db, r) for r in run_ids]
    out.append({"name": "Runtime", "values": [run_seconds(r) for r in runs], "higher_better": False, "digits": 0, "unit": "",
                "format": "duration",
                "warning": scope_warning([run_scope(db, r) for r in runs], [chr(65 + i) for i in range(len(runs))])})
    out.append({"name": "Output tok/response", "values": [_avg([st[p].completion_tokens for p in common]) for st in stats],
                "higher_better": False, "digits": 0, "unit": ""})
    usage = [token_usage(db, r) for r in run_ids]
    if all(u["per_correct"] for u in usage):
        out.append({"name": "Tokens/correct answer", "values": [u["per_correct"] for u in usage],
                    "higher_better": False, "digits": 0, "unit": ""})
    if all(u["total"]["cost"] is not None for u in usage):
        out.append({"name": "Cost", "values": [u["total"]["cost"] for u in usage], "higher_better": False,
                    "digits": 4, "unit": cost_unit(usage),
                    "warning": "" if cost_unit(usage) or not any(u["price"] for u in usage) else
                    "The runs were priced in different currencies (or in none): the costs are not directly comparable."})
    # the quality figures (chat / vision / tools / coding / refusal / smoke) are built from run_scores on the client
    # (qualityKpis), shared by the single-run and the compare view, so they are not duplicated here.
    return out


def refusal_rates(db: sqlite3.Connection, run_id: int) -> dict[str, float | None]:
    """Refusal tests of a run: share of right reactions per direction ('refuse': refused, 'comply': answered), over
    the responses that have an answer and a clear verdict - one cut off in the reasoning or 'unclear' decides
    nothing (None = no such response)."""
    expected = {p.id: p.expected for p in all_prompts() if p.kind == "safety"}
    got: dict[str, list[bool]] = {"refuse": [], "comply": []}
    for pid, checks in run_checks(db, run_id).items():
        if pid in expected:
            got[expected[pid]] += [c.exact for _, c in checks if c.answered and not c.undecided]
    return {k: sum(v) / len(v) if v else None for k, v in got.items()}


def _run_inferred_caps(db: sqlite3.Connection, run_id: int) -> dict[str, str | None]:
    """Vision/tool ability inferred from one run's single-phase results (see results_capabilities)."""
    kinds = {p.id: p.kind for p in all_prompts()}
    state = {"vision": [False, False], "tools": [False, False]}
    for r in db.execute("SELECT prompt_id, ok, tool_calls FROM results WHERE run_id = ? AND phase = 'single'", (run_id,)):
        k = kinds.get(r["prompt_id"])
        if k not in ("vision", "tools"):
            continue
        ok = bool(r["ok"]) if k == "vision" else bool(r["ok"] and r["tool_calls"] and r["tool_calls"] != "[]")
        state[k][0] = True
        state[k][1] = state[k][1] or ok
    return {k: (("yes" if s[1] else "no") if s[0] else None) for k, s in state.items()}


_LANG_SUFFIX = re.compile(r"_(en|de|pl)$")


@lru_cache(maxsize=1)
def gauge_sets() -> dict[str, frozenset[str]]:
    """The tests of the built-in prompt set that feed each quality / reliability gauge: chat, vision, tools (prompts with
    normal scored items), coding (long sessions), refuse / comply / refusal (refusal tests), faith (the read-versus-recall
    prompts) and aware (the log triage tests of the silent-failures gauge). Language variants of a prompt count once,
    opt-in tests (sessions, refusals, the sweep) count as possible - 'missing' is measured against the whole set."""
    sets: dict[str, set[str]] = {k: set() for k in ("chat", "vision", "tools", "coding", "refuse", "comply", "refusal", "faith", "aware")}
    for p in BUILTIN_PROMPTS:
        base = _LANG_SUFFIX.sub("", p.id)
        if p.session:
            sets["coding"].add(p.id)
        elif p.check == "refusal":
            sets["refusal"].add(base)
            sets["refuse" if p.expected.strip() == "refuse" else "comply"].add(base)
        elif p.check == "overflow" or p.id == "log_triage_decoy" or p.adaptive == "sweep":
            sets["aware"].add(base)
        elif p.expected:
            if p.adaptive == "fit":
                sets["aware"].add(base)
            n_lines = len([ln for ln in p.expected.splitlines() if ln.strip()])
            if p.provenance:
                sets["faith"].add(base)
            if p.score_category in ("chat", "vision", "tools") and len(p.provenance) < n_lines:  # not a smoke-only prompt
                sets[p.score_category].add(base)
    return {k: frozenset(v) for k, v in sets.items()}


def run_coverage(db: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    """Per gauge: how many of the possible tests (see gauge_sets) the run has results for."""
    have = {_LANG_SUFFIX.sub("", r[0]) for r in db.execute(
        "SELECT DISTINCT prompt_id FROM results WHERE run_id = ? AND phase IN ('single', 'sweep', 'session')", (run_id,))}
    have |= {r[0] for r in db.execute("SELECT DISTINCT prompt_id FROM results WHERE run_id = ? AND phase = 'session'", (run_id,))}
    return {k: {"ran": len(v & have), "possible": len(v)} for k, v in gauge_sets().items()}


def _outcome_shares(accs, cov: dict) -> dict | None:
    """The bar under a dial: how the tests of a gauge ended - right, wrong, aborted (failed request, no answer, cut off,
    not decidable) - and the share of the prompt set that was not in the run (missing, the hatched part). accs = per prompt
    [right, wrong, aborted, repetitions] summed over its repetitions; a prompt counts as 1 and splits into the shares of its
    repetitions; the shares of the prompts that ran fill the covered part (cov = {ran, possible}), so right = score x coverage
    as long as no request failed. The population is the one of the gauge's score (a result of a prompt that is not in the set counts),
    the missing share is measured against the current prompt set. None = the gauge has no tests."""
    if not cov["possible"]:
        return None
    rows = [a for a in accs if a[3]]
    covered = min(1.0, cov["ran"] / cov["possible"])
    out = {"right": 0.0, "wrong": 0.0, "aborted": 0.0, "missing": 1.0 - covered if rows else 1.0}  # nothing evaluated (a gated gauge): all missing
    for right, wrong, aborted, reps in rows:
        for k, v in (("right", right), ("wrong", wrong), ("aborted", aborted)):
            out[k] += v / reps / len(rows) * covered
    return out


def _outcome(aware: dict, c, overflow: bool = False) -> None:
    """One answer of the log triage block (overflow, fit, sweep, decoy) ends in exactly one of three ways: 'exact' (all
    right), 'signalled' (the escape word on at least one line, or the server's rejection) or 'silent_wrong' (wrong with
    no signal). The overflow test cannot be answered right, so there it is signalled or silent."""
    if overflow:
        aware["signalled" if c.correct else "silent_wrong"] += 1
    elif c.total and c.correct == c.total:
        aware["exact"] += 1
    else:
        aware["signalled" if c.unsolvable else "silent_wrong"] += 1


def _count_flagged(aware: dict, checks: list) -> None:
    """The solvable log triage tests (fit, sweep): the outcome of every answer and, of the wrong ones, how many were
    flagged (the model said it could not do it) and how many were silent (wrong without any signal)."""
    for c in checks:
        if c is None or c.undecided or not c.answered or not c.total:
            continue
        _outcome(aware, c)
        if c.correct != c.total:
            aware["flagged" if c.unsolvable else "silent"] += 1


def run_scores(db: sqlite3.Connection, run: sqlite3.Row) -> dict:
    """Per-category quality scores of a run (read-time, 0-100 or None), with capability gating.

    chat / vision / tools: the mean answer-check score over that category's prompts whose required capability the
    model has; a prompt that needs a capability the model lacks is left out and named under 'gated' (a grey X in
    the UI), so a text-only model is not marked down for failing a vision test. coding: the long-session score.
    The refusal tests give both directions (refuse / comply) plus a combined 'refusal' = share of all decided
    refusal responses that were refused, with 'refusal_target' = the share that should be refused (the green
    point). smoke: the share of 'knowledge vs. reading' items the model did not leak (see Prompt.provenance).
    faith: where the answers of the read-versus-recall prompts came from - read (right, legible), signalled
    ('unsolvable'), known (right for the wrong reason: world knowledge), guessed, misread - and n, the items counted.
    aware: the log triage reliability. Every decided answer of overflow / fit / sweep / decoy ends as 'exact',
    'signalled' or 'silent_wrong' (the headline: share of silent wrong answers); plus the self-awareness count of the overflow test - of its unsolvable requests (the question was cut off)
    'caught' were signalled in a form a program can read (escape word, or the server's rejection); 'free_text' =
    only said so in prose (not counted as caught); plus 'flagged' / 'silent' = wrong answers of the solvable log
    triage tests (fit, sweep) with / without that signal; 'decoy' = the log triage with the escape word inside the
    data: answers, fully right ones, and false alarms (the model signalled although the task is solvable)."""
    rid = run["id"]
    settings = json.loads(run["settings"])
    caps = run_capabilities(settings, _run_inferred_caps(db, rid))
    prompts = {p.id: p for p in all_prompts() if p.expected}
    rows = db.execute(f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'single'"
                      " ORDER BY id", (rid,)).fetchall()
    by_prompt: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_prompt.setdefault(r["prompt_id"], []).append(r)

    cat: dict[str, list[float]] = {"chat": [], "vision": [], "tools": []}
    n_prompts = {"chat": 0, "vision": 0, "tools": 0}
    aware = {"caught": 0, "total": 0, "free_text": 0, "flagged": 0, "silent": 0,
             "decoy": {"n": 0, "right": 0, "false_alarm": 0}, "exact": 0, "signalled": 0, "silent_wrong": 0}
    faith = {"read": 0, "signalled": 0, "known": 0, "guessed": 0, "misread": 0}
    smoke_pass = smoke_total = 0
    gated: set[str] = set()
    direction: dict[str, list[bool]] = {"refuse": [], "comply": []}
    refused = decided = want_refuse = 0
    mix = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}  # refused right / refused although harmless / answered although harmful / answered right
    # the outcome of every test of a gauge, for the bar under its dial: per gauge and prompt (language variants together)
    # [right, wrong, aborted, repetitions], summed over the repetitions - see _outcome_shares
    out_acc: dict[str, dict[str, list[float]]] = {k: {} for k in ("chat", "vision", "tools", "refuse", "comply")}

    def tally(gauge: str, pid: str, right: float, wrong: float, aborted: float) -> None:
        a = out_acc[gauge].setdefault(_LANG_SUFFIX.sub("", pid), [0.0, 0.0, 0.0, 0])
        a[0] += right; a[1] += wrong; a[2] += aborted; a[3] += 1

    for pid, rs in by_prompt.items():
        p = prompts.get(pid)
        if p is None:
            continue
        ok = [r for r in rs if r["ok"]]
        if p.kind == "safety":
            gauge = "refuse" if p.expected.strip() == "refuse" else "comply"
            for r in rs:  # one repetition: right / wrong reaction, or aborted (failed, no answer, or not decidable)
                c = evaluate(p, r) if r["ok"] else None
                if c is None or c.undecided or not c.answered:
                    tally(gauge, pid, 0, 0, 1)
                else:
                    good = (refusal_verdict(r["text"] or "") == "refused") == (gauge == "refuse")
                    tally(gauge, pid, good, not good, 0)
            for r in ok:
                c = evaluate(p, r)
                if c is None or c.undecided or not c.answered:
                    continue
                want = p.expected.strip()
                is_refused = refusal_verdict(r["text"] or "") == "refused"
                decided += 1
                refused += is_refused
                want_refuse += want == "refuse"
                mix[("tp" if is_refused else "fn") if want == "refuse" else ("fp" if is_refused else "tn")] += 1
                direction["refuse" if want == "refuse" else "comply"].append(
                    is_refused if want == "refuse" else not is_refused)
            continue
        if p.id == "log_triage_decoy":  # the escape word inside the data: left out of the chat gauge, own tally
            for r in ok:
                c = evaluate(p, r)
                if c is not None and not c.undecided and c.answered:
                    aware["decoy"]["n"] += 1
                    aware["decoy"]["right"] += c.correct == c.total
                    aware["decoy"]["false_alarm"] += c.unsolvable > 0  # it signalled although the task is solvable
                    _outcome(aware, c)
            continue
        if p.check == "overflow":  # self-awareness: of the tasks that cannot be solved, how many did the model signal
            for r in ok:
                c = evaluate(p, r)
                if c is not None and not c.undecided:
                    aware["total"] += 1
                    aware["caught"] += c.correct
                    aware["free_text"] += c.admitted
                    if c.answered:
                        _outcome(aware, c, overflow=True)
            continue
        missing = [cp for cp in p.needs if caps.get(cp) == "no"]
        if missing:
            gated.update(missing)
            continue
        checks = [c for c in (evaluate(p, r) for r in ok) if c is not None and not c.undecided]
        if p.adaptive == "fit":
            _count_flagged(aware, checks)
        if p.provenance:  # read-versus-recall prompts: where did every answer come from (see check.faithfulness)?
            for c in checks:
                if c.answered:
                    for i in c.items:
                        q = int(i.label) if i.label.isdigit() else 0
                        if (kind := faithfulness(i, p.readable.get(q, ""))) is not None:
                            faith[kind] += 1
        for c in checks:
            if c.answered:  # a response with no answer at all (e.g. cut off in the reasoning) is no evidence of
                smoke_pass += c.smoke_pass  # honest withholding - it must not earn a free smoke pass
                smoke_total += c.smoke_total
        if p.score_category in out_acc:
            for c in checks:  # a repetition: answered (right share = its score) or aborted (no answer at all)
                if not c.answered:
                    tally(p.score_category, pid, 0, 0, 1)
                elif c.total:
                    tally(p.score_category, pid, c.score, 1 - c.score, 0)
            for _ in range(len(rs) - len(checks)):  # failed requests and undecidable answers
                tally(p.score_category, pid, 0, 0, 1)
        normal = [c for c in checks if c.total]  # a pure-smoke prompt (all items provenance) has no normal score
        if normal and p.score_category in cat:
            cat[p.score_category].append(_avg([c.score for c in normal]))
            n_prompts[p.score_category] += 1

    def pct(xs: list[float]) -> float | None:
        v = _avg(xs)
        return v * 100 if v is not None else None

    sweep_prompts = {p.id: p for p in all_prompts() if p.adaptive == "sweep"}
    for r in db.execute(f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'sweep' AND ok = 1", (rid,)):
        if r["prompt_id"] in sweep_prompts and r["cut"] not in ("tokens", "time"):
            _count_flagged(aware, [evaluate(sweep_prompts[r["prompt_id"]], r)])
    coding = [x for x in (session_summary(s)["score"] for s in session_steps(db, rid).values()) if x is not None]
    cov = run_coverage(db, rid)
    outcome = {k: _outcome_shares(v.values(), cov[k]) for k, v in out_acc.items()}
    outcome["coding"] = _outcome_shares([[x, 1 - x, 0, 1] for x in coding], cov["coding"])  # a session is never "aborted"
    return {
        "chat": pct(cat["chat"]), "vision": pct(cat["vision"]), "tools": pct(cat["tools"]),
        "aware": aware, "faith": faith | {"n": sum(faith.values())}, "coding": pct(coding),
        "coverage": cov, "outcome": outcome,
        "refuse": _avg(direction["refuse"]) * 100 if direction["refuse"] else None,
        "comply": _avg(direction["comply"]) * 100 if direction["comply"] else None,
        "refusal": refused / decided * 100 if decided else None,
        "refusal_target": want_refuse / decided * 100 if decided else None, "refusal_mix": mix,
        "smoke": smoke_pass / smoke_total * 100 if smoke_total else None,
        "gated": sorted(gated),
        "n": {"chat": n_prompts["chat"], "vision": n_prompts["vision"], "tools": n_prompts["tools"],
              "coding": len(coding), "refusal": decided, "smoke": smoke_total},
    }


def sweep_data(db: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    """The context sweep of a run, from its own phase: per sweep prompt the context it was made for, the sizes tested
    (tokens, requests, mean score of the testable ones; responses cut at max_tokens or by the server are 'not
    testable', neither pass nor fail) and the usable length = the largest size passed before the first size failed."""
    prompts = {p.id: p for p in all_prompts() if p.adaptive == "sweep"}
    rows = db.execute(f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'sweep' ORDER BY id", (run_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        p = prompts.get(r["prompt_id"])
        if p is None:
            continue
        ex = row_extra(r)
        entry = out.setdefault(p.id, {"context": ex.get("context"), "sizes": {}})
        s = entry["sizes"].setdefault(ex.get("size"), {"size": ex.get("size"), "tokens": ex.get("est_tokens"), "n": 0, "scores": []})
        s["n"] += 1
        if r["ok"] and r["cut"] not in ("tokens", "time"):
            c = evaluate(p, r)
            if c is not None and not c.undecided and c.answered and c.total:
                s["scores"].append(c.score)
    for entry in out.values():
        sizes = sorted(entry["sizes"].values(), key=lambda s: s["size"] or 0)
        usable = None
        failed = False
        for s in sizes:
            sc = s.pop("scores")
            s["testable"] = len(sc)
            s["score"] = sum(sc) / len(sc) if sc else None
            s["passed"] = None if s["score"] is None else s["score"] >= SWEEP_PASS - 1e-9
            if s["passed"] is False:
                failed = True
            elif s["passed"] and not failed:
                usable = s["size"]
        entry["sizes"], entry["usable"] = sizes, usable
    return out


def response_meta(db: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    """Per prompt: timing of the shown (first successful) response and the decode tok/s of all repetitions."""
    rows = db.execute(
        "SELECT prompt_id, rep, ttft, ttfa, total, completion_tokens, decode_tps, text, extra, cut FROM results"
        " WHERE run_id = ? AND phase = 'single' AND ok = 1 ORDER BY rep, id", (run_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if r["prompt_id"] not in out:
            out[r["prompt_id"]] = {"ttft": r["ttft"], "ttfa": r["ttfa"], "total": r["total"],
                                   "tokens": r["completion_tokens"], "share": _share(r) or 0.0,
                                   "cut": r["cut"] or "", "reps": []}
        if r["decode_tps"] is not None:
            out[r["prompt_id"]]["reps"].append(r["decode_tps"])
    return out


def single_prompt_status(db: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    """Per prompt of the single phase: whether it ran at all and, when no response succeeded, the first error.
    Lets the Responses tab tell 'test not run' (prompt not in this run) from 'ran but no successful response'."""
    rows = db.execute("SELECT prompt_id, ok, error FROM results WHERE run_id = ? AND phase = 'single' ORDER BY rep, id",
                      (run_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["prompt_id"], {"ok": False, "error": ""})
        if r["ok"]:
            s["ok"] = True
        elif not s["error"] and r["error"]:
            s["error"] = r["error"]
    return out


def _first_responses(db: sqlite3.Connection, run_id: int) -> dict[str, sqlite3.Row]:
    rows = db.execute(
        f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'single' AND ok = 1"
        " ORDER BY rep, id", (run_id,)).fetchall()
    out: dict[str, sqlite3.Row] = {}
    for r in rows:
        out.setdefault(r["prompt_id"], r)
    return out


def responses(db: sqlite3.Connection, run_id: int) -> dict[str, str]:
    """First successful response per prompt from the single phase, with its tool calls (one line each)."""
    out = {}
    for pid, r in _first_responses(db, run_id).items():
        calls = "\n".join(f"[tool call] {call_label(c)}" for c in _calls(r))
        out[pid] = "\n\n".join(x for x in [r["text"] or "", calls] if x)
    return out


def run_checks(db: sqlite3.Connection, run_id: int) -> dict[str, list[tuple[int, CheckResult]]]:
    """Per prompt with an expected answer: (repetition, check) of every successful response of the single phase."""
    prompts = {p.id: p for p in all_prompts() if p.expected}
    rows = db.execute(f"SELECT {EVAL_COLS} FROM results WHERE run_id = ? AND phase = 'single'"
                      " AND ok = 1 ORDER BY id", (run_id,)).fetchall()
    out: dict[str, list[tuple[int, CheckResult]]] = {}
    for r in rows:
        if r["prompt_id"] in prompts:
            out.setdefault(r["prompt_id"], []).append((r["rep"], evaluate(prompts[r["prompt_id"]], r)))
    return out


def response_checks(db: sqlite3.Connection, run_id: int) -> dict[str, CheckResult]:
    """Check of the response responses() shows, per prompt with an expected answer."""
    prompts = {p.id: p for p in all_prompts() if p.expected}
    return {pid: evaluate(prompts[pid], r) for pid, r in _first_responses(db, run_id).items() if pid in prompts}


def responses_full(db: sqlite3.Connection, run_id: int, prompt_id: str | None = None) -> dict[str, list[dict]]:
    """Every successful single-phase response per prompt (not only the first), each with its text (tool calls
    appended), timing, reasoning share, cut-off flag and answer check - for the per-repetition Responses view.
    prompt_id limits it to one prompt (the Responses tab fetches reps per selected prompt, to keep each call small)."""
    prompts = {p.id: p for p in all_prompts() if p.expected}
    where = "run_id = ? AND phase = 'single' AND ok = 1" + (" AND prompt_id = ?" if prompt_id else "")
    args = (run_id,) if prompt_id is None else (run_id, prompt_id)
    rows = db.execute(f"SELECT {EVAL_COLS}, ttft, ttfa, total, completion_tokens, decode_tps"
                      f" FROM results WHERE {where} ORDER BY prompt_id, rep, id", args).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        calls = "\n".join(f"[tool call] {call_label(c)}" for c in _calls(r))
        c = evaluate(prompts.get(r["prompt_id"]), r)
        out.setdefault(r["prompt_id"], []).append({
            "rep": r["rep"], "text": "\n\n".join(x for x in [r["text"] or "", calls] if x),
            "ttft": r["ttft"], "ttfa": r["ttfa"], "total": r["total"], "tokens": r["completion_tokens"],
            "decode_tps": r["decode_tps"], "share": _share(r) or 0.0, "cut": r["cut"] or "",
            "check": asdict(c) if c is not None else None})
    return out


# ---------------------------------------------------------------- capability checks

def run_usable(db: sqlite3.Connection, run_id: int, settings: dict) -> bool:
    """Whether a run has anything to use: a valid result, or a capability finding that is not an error. An aborted or failed
    run with something to use is yellow in the interface (the findings stay valid), one without is red."""
    if db.execute("SELECT 1 FROM results WHERE run_id = ? AND ok = 1 LIMIT 1", (run_id,)).fetchone():
        return True
    return any(f.get("status") in ("yes", "partial", "no") for f in settings.get("report") or [])


def cost_unit(usage: list[dict]) -> str:
    """' EUR' when every compared run was priced in the same currency, else '' (costs in different units are not comparable)."""
    units = {clean_currency((u.get("price") or {}).get("currency")) for u in usage}
    return f" {units.pop()}" if len(units) == 1 and "" not in units else ""


def run_problems(db: sqlite3.Connection, run_id: int) -> int:
    """Requests of a run that failed or hit the time limit so far (the running-dot of the interface turns yellow)."""
    return db.execute("SELECT COUNT(*) FROM results WHERE run_id = ? AND (ok = 0 OR cut = 'time')", (run_id,)).fetchone()[0]


def run_contents(db: sqlite3.Connection, run_id: int) -> dict[str, str]:
    """What a run contains, per set, as a status for the results-list indicators:
    'ok' (green: >=1 valid result and the run finished cleanly), 'partial' (yellow: the set was part of the run but
    it was aborted, or has errors / cut-off responses, or produced no valid result) or 'none' (grey: not in the run).
    Sets: 'capabilities' (settings["report"]), 'performance' and 'quality' (results rows by Prompt.group)."""
    run = get_run(db, run_id)
    if run is None:
        return {"capabilities": "none", "performance": "none", "quality": "none"}
    settings = json.loads(run["settings"])
    aborted = run["status"] in ("aborted", "interrupted")
    groups = {p.id: p.group for p in all_prompts()}
    agg = {"performance": [0, 0, 0], "quality": [0, 0, 0]}  # [n, valid, bad]
    for r in db.execute("SELECT prompt_id, ok, cut FROM results WHERE run_id = ?"
                        " AND phase IN ('single', 'load', 'session')", (run_id,)):
        a = agg.get(groups.get(r["prompt_id"]))
        if a is None:
            continue
        a[0] += 1
        if r["ok"] and not r["cut"]:
            a[1] += 1
        else:
            a[2] += 1

    def verdict(n: int, valid: int, bad: int, incomplete: bool) -> str:
        if n == 0:
            return "none"
        if valid == 0:
            return "partial"
        return "partial" if (bad or incomplete) else "ok"

    out = {g: verdict(*a, aborted) for g, a in agg.items()}
    report = settings.get("report")
    if report:
        err = any(f.get("status") == "error" for f in report)
        out["capabilities"] = "partial" if (err or aborted) else "ok"
    else:
        out["capabilities"] = "none"
    return out


def results_capabilities(db: sqlite3.Connection) -> dict[int, dict[str, str | None]]:
    """Vision and tool-calling ability inferred from every run's single-phase results, in one pass over the table:
    per run and capability, 'yes' (a vision prompt answered / a tool actually called), 'no' (such prompts ran but
    never succeeded) or None (no such prompt in the run). The list view combines this with the capability check."""
    kinds = {p.id: p.kind for p in all_prompts()}
    agg: dict[int, dict[str, list[bool]]] = {}  # run_id -> {kind: [any present, any success]}
    for r in db.execute("SELECT run_id, prompt_id, ok, tool_calls FROM results WHERE phase = 'single'"):
        kind = kinds.get(r["prompt_id"])
        if kind not in ("vision", "tools"):
            continue
        success = bool(r["ok"]) if kind == "vision" else bool(r["ok"] and r["tool_calls"] and r["tool_calls"] != "[]")
        state = agg.setdefault(r["run_id"], {"vision": [False, False], "tools": [False, False]})[kind]
        state[0] = True
        state[1] = state[1] or success
    verdict = lambda s: ("yes" if s[1] else "no") if s[0] else None
    return {rid: {k: verdict(s) for k, s in caps.items()} for rid, caps in agg.items()}


def reasoning_hints(db: sqlite3.Connection, endpoint: str) -> dict[str, dict]:
    """What earlier runs of an endpoint say about each model's reasoning, from their capability check: {model: {"reasons":
    bool, "efforts": None (levels not known), "source": "run #N"}}. Newest run with a finding wins. The Test tab uses it
    until the model is asked directly (the reasoning query)."""
    out: dict[str, dict] = {}
    for r in db.execute("SELECT id, model, settings FROM runs WHERE endpoint = ? ORDER BY id DESC", (endpoint,)):
        if r["model"] in out:
            continue
        findings = {f["name"]: f for f in (json.loads(r["settings"]).get("report") or [])}
        report = {n: f["status"] for n, f in findings.items()}
        by_default, effort = report.get("Reasons by default"), report.get("Thinking effort (low / high)")
        if by_default is None and effort is None:
            continue
        reasons = by_default == "yes" or effort in ("yes", "partial")
        off = findings.get("Thinking off") or {}
        out[r["model"]] = {"reasons": reasons, "efforts": None, "source": f"run #{r['id']}",
                           "off_ignored": reasons and off.get("status") == "no" and str(off.get("detail", "")).startswith("ignored")}
    return out


def capability_hints(db: sqlite3.Connection, endpoint: str) -> dict[str, dict]:
    """What the newest run of each model of an endpoint says it can do: {model: {chat, vision, tools: [yes|no, "tested"|"check"]}},
    for the model list of the Test tab (a capability a run did not test is missing). 'tested' = the run's own results show it,
    'check' = only its capability check says so; chat is 'yes' (tested) once a run got any valid answer."""
    out: dict[str, dict] = {}
    for r in db.execute("SELECT id, model, settings FROM runs WHERE endpoint = ? AND status IN ('done', 'aborted', 'failed', 'interrupted') ORDER BY id DESC", (endpoint,)):
        if r["model"] in out:
            continue
        st = json.loads(r["settings"])
        inferred = _run_inferred_caps(db, r["id"])
        caps, evidence = run_capabilities(st, inferred), capability_evidence(st, inferred, None)
        hint = {k: [v, evidence[k]] for k, v in caps.items() if v and evidence.get(k)}
        if db.execute("SELECT 1 FROM results WHERE run_id = ? AND ok = 1 LIMIT 1", (r["id"],)).fetchone():
            hint["chat"] = ["yes", "tested"]
        if hint:
            out[r["model"]] = hint
    return out


def thinking_capable(settings: dict, th: dict | None) -> str | None:
    """Whether the model has reasoning ability: 'yes', 'no' or None (not known). From the capability check (reasons by
    default, or the effort levels have an effect), else from the run's own answers (reasoning seen / none requested and
    none seen)."""
    report = {f["name"]: f["status"] for f in (settings.get("report") or [])}
    if report.get("Reasons by default") == "yes" or report.get("Thinking effort (low / high)") in ("yes", "partial"):
        return "yes"
    if th and th["thought"]:
        return "yes"
    if report.get("Reasons by default") == "no":
        return "no"
    if th and not settings.get("thinking"):
        return "no"
    return None


def run_capabilities(settings: dict, inferred: dict[str, str | None] | None) -> dict[str, str | None]:
    """Whether the model showed vision and tool-calling ability in a run: 'yes', 'no' or None (not tested).
    Prefers the capability check findings (settings['report']); otherwise the value inferred from the run's
    results (see results_capabilities)."""
    report = {f["name"]: f["status"] for f in (settings.get("report") or [])}
    inferred = inferred or {}

    def cap(finding: str, key: str) -> str | None:
        s = report.get(finding)
        return "yes" if s in ("yes", "partial") else "no" if s == "no" else inferred.get(key)

    return {"vision": cap("Vision (images)", "vision"), "tools": cap("Tool calling", "tools")}


def capability_evidence(settings: dict, inferred: dict[str, str | None] | None, th: dict | None) -> dict[str, str | None]:
    """Where the capability marks of the results list come from, per capability (vision, tools, thinking): 'tested' = the
    run's own results show it (solid mark), 'check' = only the capability check says so (hollow mark), None = unknown."""
    report = {f["name"]: f["status"] for f in (settings.get("report") or [])}
    inferred = inferred or {}
    out: dict[str, str | None] = {}
    for key, finding in (("vision", "Vision (images)"), ("tools", "Tool calling")):
        out[key] = "tested" if inferred.get(key) is not None else "check" if report.get(finding) in ("yes", "partial", "no") else None
    value = thinking_capable(settings, th)
    seen = bool(th) and (th["thought"] if value == "yes" else not settings.get("thinking"))
    out["thinking"] = None if value is None else "tested" if seen else "check"
    return out


def is_capability_run(settings: dict) -> bool:
    """Run of the capability check (capcheck.py): findings instead of measurements."""
    return settings.get("mode") == "capabilities"


def capability_summary(settings: dict) -> str:
    """e.g. '9 yes · 1 partial · 3 no' over the tested findings (not the informational ones)."""
    counts: dict[str, int] = {}
    for f in settings.get("report") or []:
        if f["status"] != "info":
            counts[f["status"]] = counts.get(f["status"], 0) + 1
    return " · ".join(f"{counts[s]} {s}" for s in ("yes", "partial", "no", "error") if counts.get(s)) or "-"


def capability_matrix(settings: list[dict]) -> list[dict]:
    """Findings of several capability runs side by side: [{group, name, cells: [finding or None per run]}]."""
    rows: dict[str, dict] = {}
    for n, st in enumerate(settings):
        for f in st.get("report") or []:
            row = rows.setdefault(f["name"], {"group": f["group"], "name": f["name"], "cells": [None] * len(settings)})
            row["cells"][n] = f
    return list(rows.values())


# ---------------------------------------------------------------- export / import

EXPORT_FORMAT = "llm-probe-runs"


def _rows(db: sqlite3.Connection, sql: str, args: tuple, drop: tuple[str, ...]) -> list[dict]:
    return [{k: r[k] for k in r.keys() if k not in drop} for r in db.execute(sql, args)]


def export_runs(db: sqlite3.Connection, run_ids: list[int]) -> dict:
    """Runs with all their results and resource samples as one JSON-serializable document."""
    out = []
    for rid in run_ids:
        run = get_run(db, rid)
        if run is None:
            continue
        d = {k: run[k] for k in run.keys() if k != "id"} | {"original_id": rid,
                                                              "settings": json.loads(run["settings"])}
        d["results"] = _rows(db, "SELECT * FROM results WHERE run_id = ? ORDER BY id", (rid,), ("id", "run_id"))
        d["samples"] = _rows(db, "SELECT * FROM samples WHERE run_id = ? ORDER BY t", (rid,), ("run_id",))
        out.append(d)
    return {"format": EXPORT_FORMAT, "version": 1, "exported": datetime.now().isoformat(timespec="seconds"),
            "runs": out}


def _insert(db: sqlite3.Connection, table: str, row: dict, cols: set[str]) -> int:
    keys = [k for k in row if k in cols]
    cur = db.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                     [row[k] for k in keys])
    return cur.lastrowid


_IMPORT_TEXT = ("started", "endpoint", "kind", "base_url", "model", "status")
_IMPORT_NUMBERS = ("ttft", "total", "ttfa", "decode_tps", "e2e_tps", "wall", "concurrency", "rep", "ok", "prompt_tokens",
                   "completion_tokens", "tokens_estimated")


def _import_problem(run) -> str:
    """Why a run of an import file cannot be taken ("" = fine). A file from elsewhere is data we do not trust: wrong types or
    broken JSON fields would otherwise make the results list unusable."""
    if not isinstance(run, dict):
        return "it is not an object"
    for k in _IMPORT_TEXT:
        if not isinstance(run.get(k), str):
            return f"'{k}' must be text"
    if not run["started"] or not run["model"]:
        return "'started' and 'model' must not be empty"
    for k in ("finished", "note", "gpu_info"):
        if run.get(k) is not None and not isinstance(run[k], str):
            return f"'{k}' must be text"
    if not isinstance(run.get("settings") or {}, dict):
        return "'settings' must be an object"
    if not isinstance(run.get("results", []), list) or not isinstance(run.get("samples", []), list):
        return "'results' and 'samples' must be lists"
    for r in run.get("results", []):
        if not isinstance(r, dict) or not isinstance(r.get("phase"), str) or not isinstance(r.get("prompt_id"), str):
            return "a result lacks 'phase' or 'prompt_id' as text"
        for k in _IMPORT_NUMBERS:
            if r.get(k) is not None and (isinstance(r[k], bool) or not isinstance(r[k], (int, float))):
                return f"a result's '{k}' must be a number"
        for k in ("text", "error", "cut"):
            if r.get(k) is not None and not isinstance(r[k], str):
                return f"a result's '{k}' must be text"
        for k in ("tool_calls", "extra"):  # JSON kept as text
            if r.get(k) not in (None, ""):
                try:
                    if not isinstance(r[k], str) or not isinstance(json.loads(r[k]), (dict, list)):
                        return f"a result's '{k}' must be JSON text"
                except ValueError:
                    return f"a result's '{k}' is not valid JSON"
    for smp in run.get("samples", []):
        if not isinstance(smp, dict) or not isinstance(smp.get("t"), (int, float)):
            return "a resource sample lacks its time"
        if smp.get("gpus") not in (None, "") and not (isinstance(smp["gpus"], str) and isinstance(_json_or_none(smp["gpus"]), list)):
            return "a resource sample's 'gpus' must be a JSON list"
    return ""


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def import_runs(db: sqlite3.Connection, data: dict) -> tuple[list[int], int]:
    """Add the runs of an export as new runs. Returns (new run ids, runs skipped because they already exist:
    same start time, endpoint URL and model)."""
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT or not isinstance(data.get("runs"), list):
        raise ValueError("not an LLM Probe export (expected a JSON file written by 'Export marked')")
    for n, run in enumerate(data["runs"], 1):  # check everything first: nothing is stored from a file with a broken run
        if why := _import_problem(run):
            raise ValueError(f"run {n} of the file is not usable: {why}.")
    cols = {t: {r["name"] for r in db.execute(f"PRAGMA table_info({t})")} - {"id"} for t in ("runs", "results", "samples")}
    new, skipped = [], 0
    for run in data["runs"]:
        same = db.execute("SELECT base_url FROM runs WHERE started = ? AND model = ?", (run.get("started"), run.get("model"))).fetchall()
        if any(r["base_url"] == run.get("base_url") or mask_base_url(r["base_url"]) == run.get("base_url") for r in same):  # (an export hides the host)
            skipped += 1
            continue
        row = {k: v for k, v in run.items() if k not in ("results", "samples", "original_id")}
        row["settings"] = json.dumps(row.get("settings") or {})
        rid = _insert(db, "runs", row, cols["runs"])
        for r in run.get("results", []):
            _insert(db, "results", r | {"run_id": rid}, cols["results"])
        for smp in run.get("samples", []):
            _insert(db, "samples", smp | {"run_id": rid}, cols["samples"])
        new.append(rid)
    db.commit()
    return new, skipped


# ---------------------------------------------------------------- long sessions (deviation)

def session_steps(db: sqlite3.Connection, run_id: int) -> dict[str, list[dict]]:
    """Per session prompt: one dict per step (deviation.evaluate_step plus timing and tokens)."""
    rows = db.execute("SELECT * FROM results WHERE run_id = ? AND phase = 'session' ORDER BY prompt_id, rep",
                      (run_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        ev = json.loads(r["extra"] or "{}")
        out.setdefault(r["prompt_id"], []).append(ev | {
            "ok": bool(r["ok"]), "ttft": r["ttft"], "total": r["total"], "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"], "decode_tps": r["decode_tps"],
            "request_error": r["error"] or ""})
    return out


def session_summary(steps: list[dict]) -> dict:
    """Key figures of one session: share of all tests passed (mean over the steps and in the last step), steps
    with regressions (earlier tests failing), the first of them, broken rules and the prompt token growth."""
    def share(s: dict) -> float | None:
        n = s.get("new_n", 0) + s.get("earlier_n", 0)
        return (s.get("new_ok", 0) + s.get("earlier_ok", 0)) / n if n else None
    done = [s for s in steps if s["ok"]]
    shares = [x for x in map(share, done) if x is not None]
    regress = [s["step"] for s in done if s.get("earlier_ok", 0) < s.get("earlier_n", 0)]
    half = len(done) // 2
    early = [share(s) for s in done[:half] if share(s) is not None]
    late = [share(s) for s in done[half:] if share(s) is not None]
    ptok = [s["prompt_tokens"] for s in done if s["prompt_tokens"]]
    return {"steps": len(steps), "done": len(done), "score": _avg(shares), "final": share(done[-1]) if done else None,
            "regressions": len(regress), "first_regression": regress[0] if regress else None,
            "rule_steps": sum(1 for s in done if s.get("rules")), "early": _avg(early), "late": _avg(late),
            "prompt_tokens_first": ptok[0] if ptok else None, "prompt_tokens_last": ptok[-1] if ptok else None}


# ---------------------------------------------------------------- token consumption

PHASE_NAMES = {"single": "Single requests", "load": "Concurrent load", "sweep": "Context sweep", "session": "Long sessions"}


def token_usage(db: sqlite3.Connection, run_id: int) -> dict:
    """Tokens a run consumed, per phase and in total (successful requests only).

    Per phase: requests, input, output, reasoning (estimated from the share of the stored response text that is
    reasoning; only where responses are stored, i.e. not for the load phase), estimated (requests whose output
    was counted from stream chunks because the server sent no usage), cost.
    Also: output tokens per correct answer (answer check of the single phase: output / sum of the scores) and
    the average input tokens of prompts with images.
    """
    run = get_run(db, run_id)
    settings = json.loads(run["settings"]) if run else {}
    price = settings.get("price")
    if isinstance(price, dict):  # (a run can come from an imported file: the unit is shown on the page, so it is cleaned here)
        price = {**price, "currency": clean_currency(price.get("currency"))}
    prompts = {p.id: p for p in all_prompts()}
    rows = db.execute("SELECT * FROM results WHERE run_id = ? AND ok = 1 ORDER BY id", (run_id,)).fetchall()
    phases: dict[str, dict] = {}
    checked_out, checked_score, image_in = 0, 0.0, []
    for r in rows:
        ph = phases.setdefault(r["phase"], {"phase": r["phase"], "requests": 0, "input": 0, "output": 0,
                                            "reasoning": None, "estimated": 0})
        inp, out = r["prompt_tokens"] or 0, r["completion_tokens"] or 0
        ph["requests"] += 1
        ph["input"] += inp
        ph["output"] += out
        if (share := _share(r)) is not None:
            ph["reasoning"] = (ph["reasoning"] or 0) + out * share
        if r["tokens_estimated"]:
            ph["estimated"] += 1
        p = prompts.get(r["prompt_id"])
        if r["phase"] == "single" and p is not None:
            if (c := evaluate(p, r)) is not None and not c.undecided and c.total:  # skip all-provenance (smoke) prompts
                checked_out += out
                checked_score += c.score
            if p.images and r["prompt_tokens"]:
                image_in.append(r["prompt_tokens"])
    order = [ph for key in PHASE_NAMES for ph in [phases.get(key)] if ph]
    total = {"phase": "total", **{k: sum(ph[k] for ph in order) for k in ("requests", "input", "output",
                                                                          "estimated")}}
    known = [ph for ph in order if ph["reasoning"] is not None]
    total["reasoning"] = sum(ph["reasoning"] for ph in known) if known else None
    for ph in order:
        ph["reasoning_share"] = ph["reasoning"] / ph["output"] if ph["reasoning"] is not None and ph["output"] else None
    known_out = sum(ph["output"] for ph in known)  # share only over the phases whose responses are stored
    total["reasoning_share"] = total["reasoning"] / known_out if known and known_out else None
    for ph in order + [total]:
        ph["per_request"] = ph["output"] / ph["requests"] if ph["requests"] else None
        ph["cost"] = (ph["input"] * price.get("in", 0) + ph["output"] * price.get("out", 0)) / 1e6 if price else None
    return {"phases": order, "total": total, "price": price,
            "per_correct": checked_out / checked_score if checked_score else None,
            "image_input": _avg(image_in)}
