"""Simple web UI (--server): a JSON API plus one static page, served in-process by aiohttp."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import html
import json
import os
import re
import socket
import time
import webbrowser
from urllib.parse import urlsplit
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx
from aiohttp import web
from rich.errors import MarkupError
from rich.text import Text

from . import __version__, placement, storage
from .bench import BatchRunner, result_line
from .deviation import TASKS
from . import overlap, sandbox
from .capcheck import probe_reasoning
from .client import RequestResult, api_error_hint, list_models, model_contexts, model_details
from .config import (DEFAULT_PORTS, clean_currency, MAX_BATCH_RUNS, BenchSettings, Endpoint, batch_plan, edit_config, load_config,
                     parse_values, split_values, valid_thinking)
from .lock import TestLock, holder, holder_text
from .monitor import test_ssh
from .privacy import host_of, mask_data
from .prompts import (CHECK_MODES, DEFAULT_SELECTION, DIFFICULTIES, IMAGES_ASSETS, LANGUAGES, PROMPTSET_VERSION, Prompt, all_prompts,
                      edit_custom_prompts, load_custom_prompts, SAFETY_NOTICE, SAFETY_WHERE, image_exists, image_path, save_image, upload_path, selection_facets)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PARTS = {f.name: f for f in STATIC_DIR.iterdir() if f.suffix in (".js", ".css")}  # the style sheet and scripts the page links
MAX_LOG = 2000

routes = web.RouteTableDef()


# ------------------------------------------------------------------ Helpers

def markup_to_html(msg: str) -> str:
    """Convert the Rich markup used in benchmark log messages to escaped HTML spans."""
    try:
        text = Text.from_markup(msg)
    except MarkupError:  # e.g. brackets in an error message
        return html.escape(msg)
    classes: list[set[str]] = [set() for _ in text.plain]
    for span in text.spans:
        style = str(span.style)
        names = {c for c in ("red", "yellow", "green") if c in style}
        if "b" in style.split() or "bold" in style:
            names.add("bold")
        if "dim" in style:
            names.add("dim")
        for i in range(span.start, min(span.end, len(classes))):
            classes[i] |= names
    out, i, plain = [], 0, text.plain
    while i < len(plain):
        j = i
        while j < len(plain) and classes[j] == classes[i]:
            j += 1
        chunk = html.escape(plain[i:j])
        out.append(f'<span class="{" ".join(sorted(classes[i]))}">{chunk}</span>' if classes[i] else chunk)
        i = j
    return "".join(out)


def endpoint_public(e: Endpoint) -> dict:
    """Endpoint without secrets; the browser only learns whether one is set."""
    d = asdict(e)
    d["has_api_key"], d["has_ssh_password"] = bool(e.api_key), bool(e.ssh_password)
    d["api_key"] = d["ssh_password"] = ""
    return d


def endpoint_from_body(body: dict, old: Endpoint | None) -> Endpoint:
    """Build an endpoint from form data; empty secret fields keep the stored value unless 'clear_secrets' is set."""
    keep = old is not None and not body.get("clear_secrets")
    # A stored secret belongs to the server it was entered for: when the URL (API key) or the SSH host, port or user (SSH
    # password) changes, it is NOT carried over - it has to be entered again. Otherwise a changed URL could send it elsewhere.
    new_url = str(body.get("base_url", "")).strip()
    keep_key = keep and new_url == old.base_url
    keep_ssh = keep and (str(body.get("ssh_host", "")).strip(), str(body.get("ssh_user", "")).strip()) == (old.ssh_host, old.ssh_user) \
        and str(body.get("ssh_port") or 22) == str(old.ssh_port)
    try:
        port = int(body.get("ssh_port") or 22)
    except ValueError:
        port = 22
    try:
        num_ctx = max(0, int(body.get("num_ctx") or 0))
    except ValueError:
        num_ctx = 0

    def price(key: str) -> float:
        try:
            return max(0.0, float(str(body.get(key) or 0).replace(",", ".")))
        except ValueError:
            return 0.0

    return Endpoint(
        name=str(body.get("name", "")).strip(),
        kind=body.get("kind") if body.get("kind") in ("ollama", "vllm", "openwebui", "openai") else "openai",
        base_url=str(body.get("base_url", "")).strip(),
        api_key=str(body.get("api_key") or "").strip() or (old.api_key if keep_key else ""),
        model=old.model if old else "",
        ssh_host=str(body.get("ssh_host", "")).strip(),
        ssh_port=port,
        ssh_user=str(body.get("ssh_user", "")).strip(),
        ssh_password=str(body.get("ssh_password") or "") or (old.ssh_password if keep_ssh else ""),
        ssh_key_file=str(body.get("ssh_key_file", "")).strip(),
        num_ctx=num_ctx,
        price_in=price("price_in"),
        price_out=price("price_out"),
        currency=clean_currency(body.get("currency")),
    )


def url_problem(e: Endpoint) -> str:
    """Why the endpoint's URL cannot be used ("" = fine): only http and https servers are contacted (no file:, ftp: ...)."""
    u = urlsplit(e.base_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "The URL must start with http:// or https:// and name a server."
    return "Put the API key into its own field, not into the URL (user:password@ would end up in logs and exports)." if u.username or u.password else ""


def settings_from_body(body: dict) -> BenchSettings:
    d = BenchSettings()

    def num(key: str, cast, default):
        try:
            return cast(body.get(key, default))
        except (TypeError, ValueError):
            return default

    raw_levels = body.get("concurrency_levels", "")
    if isinstance(raw_levels, list):
        raw_levels = ",".join(map(str, raw_levels))
    levels = sorted({int(x) for x in str(raw_levels).replace(" ", "").split(",") if x.isdigit() and int(x) > 0})
    # batch run: a setting may carry several comma separated values (a list from the multi-select for thinking); the
    # first is the scalar the run is known by, the list (only when there are several) spans the batch
    max_tokens = parse_values(body.get("max_tokens", ""), int, "max_tokens", 1)
    temps = parse_values(body.get("temperature", ""), float, "temperature", 0)
    num_ctx = parse_values(body.get("num_ctx", ""), int, "num_ctx", 128)
    timeouts = parse_values(body.get("request_timeout", ""), float, "request_timeout", 1)
    raw_think = body.get("thinking", "")
    think = [t for t in dict.fromkeys(raw_think if isinstance(raw_think, list) else [raw_think]) if valid_thinking(t)]
    return BenchSettings(
        max_tokens=max_tokens[0] if max_tokens else d.max_tokens,
        max_tokens_values=max_tokens if len(max_tokens) > 1 else [],
        temperature=temps[0] if temps else d.temperature,
        temperature_values=temps if len(temps) > 1 else [],
        num_ctx_values=num_ctx,  # (a single value counts, too: it overrides the endpoint's num_ctx; several make a batch)
        repetitions=max(1, num("repetitions", int, d.repetitions)),
        concurrency_levels=levels or d.concurrency_levels,
        requests_per_level=max(1, num("requests_per_level", int, d.requests_per_level)),
        request_timeout=timeouts[0] if timeouts else d.request_timeout,
        request_timeout_values=timeouts if len(timeouts) > 1 else [],
        run_single=bool(body.get("run_single", True)),
        run_concurrency=bool(body.get("run_concurrency", True)),
        thinking=think[0] if think else "",
        thinking_values=think if len(think) > 1 else [],
        escape_word="random" if body.get("escape_word") == "random" else "",
    )


# the Whole test settings a model may have of its own in the Test tab (anything else sent is ignored)
MODEL_PARAM_KEYS = ("max_tokens", "temperature", "thinking", "num_ctx", "request_timeout")


def clean_model_params(raw) -> dict[str, dict]:
    """{model: {setting: value}} of the overrides that are really set; an empty value means "like whole test"."""
    out = {}
    for model, params in (raw if isinstance(raw, dict) else {}).items():
        keep = {k: v for k, v in (params if isinstance(params, dict) else {}).items()
                if k in MODEL_PARAM_KEYS and v not in ("", None, [])}
        if keep:
            out[str(model)] = keep
    return out


def mask_hosts(request: web.Request, data, runs: list[dict] | None = None):
    """Host names are private: unless the request says `hosts=1` (the user ticked "Include host names"), the addresses of the
    servers are replaced by <host> in what is sent to the page for a printed report (`mask=1`) or written to an export file
    (the endpoint name and the model stay). `runs`: the run rows whose base_url / endpoint's SSH host are to be hidden."""
    if request.query.get("hosts") == "1":
        return data
    cfg = load_config()
    hosts = set()
    for r in runs or []:
        hosts.add(host_of(r.get("base_url", "")))
        if (e := cfg.endpoint(r.get("endpoint", ""))) is not None:
            hosts.update({host_of(e.base_url), (e.ssh_host or "").lower()})
    return mask_data(data, hosts - {"", "localhost", "127.0.0.1", "::1"})


def sweep_interrupted(request: web.Request, db) -> None:
    """A run that says 'running' while nobody runs a test (this server's own test counts) was cut off: show it as interrupted."""
    if not request.app["bench"].running and not holder():
        storage.mark_interrupted(db)


def error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def run_detail(db, run_id: int) -> dict | None:
    run = storage.get_run(db, run_id)
    if run is None:
        return None
    settings = json.loads(run["settings"])
    th = storage.thinking_stats(db, run_id)
    return {
        "run": dict(run) | {"settings": settings, "context_label": storage.context_label(settings),
                            "problems": storage.run_problems(db, run_id) if run["status"] == "running" else 0,
                            "placement_label": placement.label(settings.get("placement")),
                            "thinking_label": storage.thinking_label(settings, th, short=True),
                            "server_lines": [{"name": n, "text": t, "warn": w}
                                             for n, t, w in storage.server_lines(
                                                 settings, th, storage.cut_label(db, run_id, settings))]},
        "single": [asdict(s) for s in storage.single_stats(db, run_id)],
        "reps": storage.single_reps(db, run_id),
        "sweep": storage.sweep_data(db, run_id),
        "load": [asdict(s) for s in storage.load_stats(db, run_id)],
        "resources": [asdict(s) for s in storage.resource_stats(db, run_id)],
        "tokens": storage.token_usage(db, run_id),
        "refusals": storage.refusal_rates(db, run_id),
        "scores": storage.run_scores(db, run),
        "checks": {pid: [{"rep": rep} | asdict(c) for rep, c in checks]
                   for pid, checks in storage.run_checks(db, run_id).items()},
        "sessions": {pid: {"steps": steps, "summary": storage.session_summary(steps)}
                     for pid, steps in storage.session_steps(db, run_id).items()},
    }


# ------------------------------------------------------------- Benchmark state

STREAMED = -2  # "out" of a poll: the client follows the live output over /api/bench/stream
BURST_CHUNKS = 100  # more chunks than this within BURST_WINDOW seconds (1000 per second) are sent in groups ...
BURST_WINDOW = 0.1
GROUP_SECONDS = 0.01  # ... every 10 ms (the usual speed of a model is far below: every chunk goes out at once)

BOOT_ID = os.urandom(4).hex()  # changes with every start of the server: an open page sees that it was restarted (maybe updated)
QUIET_PHASES = ("warmup", "capabilities", "load")  # phases without live output (the page shows why; see PHASE_NOTE in test.js)


class BenchState:
    """The single benchmark the server can run at a time, plus its live output for polling clients."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.log: list[dict] = []
        self.seq = 0
        self.done = 0
        self.total = 0
        self.sample: dict | None = None
        self.run_id: int | None = None
        self.status = ""
        self.title = ""
        self.phase = ""  # what the test does now (the live output has something to show in "single" and "session")
        self.out = {"id": 0, "title": ""}  # the current request; its text is in `parts`
        self.parts: dict[str, list[str]] = {"think": [], "text": []}
        self.watchers: set[asyncio.Queue] = set()  # the open live output streams (/api/bench/stream)
        self.burst = {"start": 0.0, "n": 0}  # chunks within the current BURST_WINDOW
        self.pending: list[tuple[str, str]] = []  # chunks not sent to the streams yet (only while a burst is grouped)
        self.timer: asyncio.TimerHandle | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def write(self, msg: str) -> None:
        self.seq += 1
        self.log.append({"seq": self.seq, "html": markup_to_html(msg)})
        del self.log[:-MAX_LOG]

    def emit(self, kind: str, **d) -> None:
        if kind == "log":
            self.write(d["msg"])
        elif kind == "progress":
            self.done, self.total = d["done"], d["total"]
        elif kind == "sample":
            self.sample = {k: v for k, v in asdict(d["sample"]).items() if k != "t"}
        elif kind == "result":
            self.write(result_line(d["prompt"], d["rep"], d["result"], d.get("word", "")))
        elif kind == "phase":
            self.phase = d["name"]
            if d["name"] in QUIET_PHASES:  # nothing streams now: a stream opened or reopened later must not replay the last request
                self.flush()
                self.out = {"id": self.out["id"] + 1, "title": ""}
                self.parts = {"think": [], "text": []}
        elif kind == "stream_start":
            self.flush()
            self.out = {"id": self.out["id"] + 1, "title": d["title"]}
            self.parts = {"think": [], "text": []}
            self.push({"k": "start"} | self.output())
        elif kind == "stream":
            self.parts["think"].append(d["think"])
            self.parts["text"].append(d["text"])
            self.chunk(d["think"], d["text"])
        elif kind == "finished":
            self.run_id, self.status = d["run_id"], d["status"]
            style = "green" if d["status"] == "done" else "yellow" if d.get("usable") else "red"  # an abort or failure keeps what came in
            self.write(f"[{style}]Run #{d['run_id']} finished: {d['status']}[/] - see Results/Compare.")

    def push(self, event: dict | None) -> None:
        for q in self.watchers:
            q.put_nowait(event)

    def output(self) -> dict:
        """The live output of the current request: id, title and the text so far."""
        return self.out | {"think": "".join(self.parts["think"]), "text": "".join(self.parts["text"])}

    def chunk(self, think: str, text: str) -> None:
        """Send one chunk of the model to the open streams at once. Only a burst (a model that streams thousands of tokens per
        second) is sent in groups every GROUP_SECONDS, so that showing the output cannot slow the measurement down."""
        if not self.watchers:
            return
        now = time.monotonic()
        if now - self.burst["start"] > BURST_WINDOW:
            self.burst = {"start": now, "n": 0}
        self.burst["n"] += 1
        self.pending.append((think, text))
        if self.burst["n"] <= BURST_CHUNKS and self.timer is None:
            self.flush()
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(GROUP_SECONDS, self.flush)

    def flush(self) -> None:
        """Send the grouped chunks."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.pending:
            self.push({"k": "delta", "think": "".join(t for t, _ in self.pending), "text": "".join(x for _, x in self.pending)})
            self.pending = []

    def watch(self) -> asyncio.Queue:
        """A queue for one live output stream; it starts with the output so far."""
        self.flush()
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait({"k": "start"} | self.output())
        self.watchers.add(q)
        return q

    def start(self, runner: BatchRunner, title: str, lock: TestLock | None = None) -> None:
        """Run the test; lock (held by the caller) is released when it ends."""
        self.log, self.done, self.total = [], 0, runner.total_steps
        self.sample, self.run_id, self.status, self.title = None, None, "running", title
        self.flush()
        self.out = {"id": self.out["id"] + 1, "title": ""}
        self.parts = {"think": [], "text": []}
        self.phase = ""
        self.push({"k": "start"} | self.output())
        self.write(title)

        async def guarded() -> None:
            try:
                await runner.run()
            except asyncio.CancelledError:
                pass
            except Exception as e:  # runner normally handles its own errors
                self.status = "failed"
                self.write(f"[red]Internal error:[/] {Text(str(e)).markup}")
            finally:
                if lock:
                    lock.release()

        self.task = asyncio.create_task(guarded())

    def snapshot(self, since: int, out_id: int = -1, think_len: int = 0, text_len: int = 0) -> dict:
        """State for a polling client; the live output is sent as the part the client does not have yet."""
        if out_id == STREAMED:  # the client gets the output over the stream
            out = self.out | {"think": "", "text": "", "append": True}
        elif out_id == self.out["id"]:
            out = self.out | {"think": "".join(self.parts["think"])[think_len:], "text": "".join(self.parts["text"])[text_len:], "append": True}
        else:
            out = self.output()
        return {
            "running": self.running, "status": self.status, "title": self.title, "run_id": self.run_id, "phase": self.phase,
            "done": self.done, "total": self.total, "sample": self.sample,
            "log": [x for x in self.log if x["seq"] > since], "seq": self.seq, "out": out, "boot": BOOT_ID,
            # a test started by another process (command line or a second server) on the same data
            "other": None if self.running else (lambda h: holder_text(h) if h else None)(holder()),
        }


# --------------------------------------------------------------------- Routes

@routes.get("/")
async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get(r"/static/{name:[A-Za-z0-9_-]+\.(?:js|css)}")
async def static_file(request: web.Request) -> web.StreamResponse:
    """The parts of the page (style sheet, scripts): only the files that are in the static folder, looked up by name in a list made at
    start - the name is never turned into a path. no-cache: the browser asks again, so an update is never shown with old scripts."""
    path = STATIC_PARTS.get(request.match_info["name"])
    if path is None:
        return error("Unknown file.", 404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


@routes.get("/api/config")
async def get_config(_: web.Request) -> web.Response:
    cfg = load_config()
    return web.json_response({
        "endpoints": [endpoint_public(e) for e in cfg.endpoints],
        "bench": asdict(cfg.bench),
        "default_ports": DEFAULT_PORTS,
    })


@routes.post("/api/endpoints")
async def save_endpoint(request: web.Request) -> web.Response:
    body = await request.json()
    with edit_config() as cfg:
        original = body.get("original") or None
        old = cfg.endpoint(original) if original else None
        if original and old is None:
            return error(f"Endpoint '{original}' no longer exists.", 404)
        e = endpoint_from_body(body, old)
        if not e.name or not e.base_url:
            return error("Name and URL are required.")
        if why := url_problem(e):
            return error(why)
        dup = cfg.endpoint(e.name)
        if dup and dup is not old:
            return error(f"An endpoint named '{e.name}' already exists.")
        if old:
            cfg.endpoints[cfg.endpoints.index(old)] = e
        else:
            cfg.endpoints.append(e)
    return web.json_response({"ok": True, "endpoint": endpoint_public(e)})


@routes.delete("/api/endpoints/{name}")
async def delete_endpoint(request: web.Request) -> web.Response:
    with edit_config() as cfg:
        e = cfg.endpoint(request.match_info["name"])
        if e is None:
            return error("Unknown endpoint.", 404)
        cfg.endpoints.remove(e)
    return web.json_response({"ok": True})


def _form_endpoint(body: dict) -> Endpoint:
    original = body.get("original") or None
    return endpoint_from_body(body, load_config().endpoint(original) if original else None)


@routes.post("/api/endpoints/test-api")
async def test_api(request: web.Request) -> web.Response:
    e = _form_endpoint(await request.json())
    if why := url_problem(e):
        return web.json_response({"ok": False, "message": why})
    try:
        models = await list_models(e)
    except Exception as ex:
        return web.json_response({"ok": False, "message": f"API not reachable: {type(ex).__name__}: {ex}\n"
                                                           + api_error_hint(ex)})
    return web.json_response({"ok": True, "message": f"API reachable. {len(models)} model(s): " + ", ".join(models)})


@routes.post("/api/endpoints/test-ssh")
async def test_ssh_route(request: web.Request) -> web.Response:
    e = _form_endpoint(await request.json())
    if not e.ssh_host:
        return web.json_response({"ok": False, "message": "No SSH host set (monitoring is off)."})
    try:
        out = await test_ssh(e)
    except Exception as ex:
        return web.json_response({"ok": False, "message": f"SSH failed: {type(ex).__name__}: {ex}"})
    return web.json_response({"ok": True, "message": "SSH ok:\n" + out})


@routes.get("/api/models")
async def models(request: web.Request) -> web.Response:
    ep = load_config().endpoint(request.query.get("endpoint", ""))
    if ep is None:
        return error("Unknown endpoint.", 404)
    try:
        names = await list_models(ep)
    except Exception as ex:
        return error(f"{ep.name}: cannot list models ({type(ex).__name__}). {api_error_hint(ex)}", 502)
    cfg = load_config()
    hints = stored_hints(cfg, ep.name, names)
    return web.json_response({"models": names, "selected": ep.selected_models,
                              "params": cfg.model_params.get(ep.name) or {}} | hints)


def stored_hints(cfg, endpoint: str, names: list[str]) -> dict:
    """What is known about the models of an endpoint without asking the server: the reasoning and capabilities that earlier
    runs found (the model's own reasoning query overrides it)."""
    db = storage.connect()
    try:
        reasoning = storage.reasoning_hints(db, endpoint)
        caps = storage.capability_hints(db, endpoint)
    finally:
        db.close()
    reasoning.update({m: r | {"source": "query"} for m, r in (cfg.reasoning.get(endpoint) or {}).items() if m in names})
    return {"reasoning": {m: reasoning[m] for m in names if m in reasoning}, "capabilities": {m: caps[m] for m in names if m in caps}}


@routes.get("/api/models/hints")
async def model_hints(request: web.Request) -> web.Response:
    """The stored part of /api/models only (no request to the endpoint): the Test tab asks again when the runs changed."""
    cfg = load_config()
    if cfg.endpoint(request.query.get("endpoint", "")) is None:
        return error("Unknown endpoint.", 404)
    return web.json_response(stored_hints(cfg, request.query["endpoint"], request.query.getall("model", [])))


_DETAILS_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}  # (endpoint, url, model) -> (time, details)
DETAILS_TTL = 600.0  # seconds; a model's quantization does not change while it is listed
DETAILS_BUDGET = 6.0  # seconds the whole list may take; models not reached are asked again at the next call


@routes.get("/api/models/details")
async def model_info(request: web.Request) -> web.Response:
    """Quantization and parameter size of the models of an endpoint (the server's own statement where it makes one,
    else read from the model name; see client.model_details). Cached; asked in parallel, so a list of dozens of
    models takes about as long as one query."""
    ep = load_config().endpoint(request.query.get("endpoint", ""))
    if ep is None:
        return error("Unknown endpoint.", 404)
    names = [m for m in request.query.getall("model", []) if m]
    now = time.monotonic()
    out: dict[str, dict] = {}
    todo = []
    for m in names:
        hit = _DETAILS_CACHE.get((ep.name, ep.base_url, m))
        if hit and now - hit[0] < DETAILS_TTL:
            out[m] = hit[1]
        else:
            todo.append(m)
    sem = asyncio.Semaphore(8)

    async def one(m: str) -> None:
        async with sem:
            _DETAILS_CACHE[(ep.name, ep.base_url, m)] = (time.monotonic(), out.setdefault(m, await model_details(ep, m)))

    if todo:
        await asyncio.wait([asyncio.create_task(one(m)) for m in todo], timeout=DETAILS_BUDGET)
    if not ep.ollama_like:  # the context a vLLM-like server runs the model with: shown read-only (it cannot be set from here)
        contexts = await model_contexts(ep)
        out = {m: d | {"context": contexts.get(m)} for m, d in out.items()}
    return web.json_response({"details": out, "missing": [m for m in names if m not in out]})


_TIMING_CACHE: dict[str, tuple[str, dict]] = {}  # endpoint -> (runs version, profile)


@routes.get("/api/models/timing")
async def model_timing(request: web.Request) -> web.Response:
    """What the earlier runs of an endpoint's models took (newest first, at most ~3 s of reading): the browser builds the
    estimated run time of the selected test from it. Cached until the run list changes."""
    name = request.query.get("endpoint", "")
    if load_config().endpoint(name) is None:
        return error("Unknown endpoint.", 404)

    def build() -> dict:
        db = storage.connect()
        try:
            version = storage.runs_version(db)
            if (hit := _TIMING_CACHE.get(name)) and hit[0] == version:
                return hit[1]
            profile = storage.timing_profile(db, name)
            _TIMING_CACHE[name] = (version, profile)
            return profile
        finally:
            db.close()

    return web.json_response(await asyncio.to_thread(build))


@routes.post("/api/models/reasoning")
async def query_reasoning(request: web.Request) -> web.Response:
    """The reasoning query of one model (the Q button of the Test tab): sends a few tiny requests, which loads the model on
    the endpoint, and remembers what the model accepts."""
    body = await request.json()
    ep, model = load_config().endpoint(str(body.get("endpoint") or "")), str(body.get("model") or "")
    if ep is None or not model:
        return error("Select an endpoint and a model.")
    if request.app["bench"].running or holder():
        return error("A test is running - the query would load the model on the endpoint and disturb it.", 409)
    try:
        result = await probe_reasoning(ep, model)
    except (RuntimeError, httpx.HTTPError, OSError) as ex:
        return error(f"{ep.name} / {model}: the query failed ({type(ex).__name__}: {ex}). {api_error_hint(ex)}", 502)
    with edit_config() as cfg:
        cfg.reasoning.setdefault(ep.name, {})[model] = result
    return web.json_response({"ok": True, "reasoning": result | {"source": result["source"] if result["source"] == "declared" else "query"}})


@routes.get("/api/prompts")
async def get_prompts(_: web.Request) -> web.Response:
    return web.json_response([asdict(p) | {"needs": p.needs, "group": p.group, "kind": p.kind, "score_category": p.score_category, "steps": [s.request for s in TASKS[p.session["task"]].steps]
                                           if p.session else []} for p in all_prompts()])


@routes.get("/api/safety-notice")
async def safety_notice(request: web.Request) -> web.Response:
    """Text of the notice before refusal tests run against an endpoint (whether it looks like a public service)."""
    ep = load_config().endpoint(request.query.get("endpoint", ""))
    if ep is None:
        return web.json_response({"text": SAFETY_NOTICE.format(where=""), "public": None})
    public, host = await overlap.public_host(ep)
    return web.json_response({"text": SAFETY_NOTICE.format(where=SAFETY_WHERE[public].format(host=host)),
                              "public": public})


@routes.get("/api/selection")
async def get_selection(_: web.Request) -> web.Response:
    """Facets of the prompt selection (language, group, kind) with their labels, and the default selection."""
    return web.json_response({"facets": selection_facets(all_prompts()), "default": DEFAULT_SELECTION,
                             "capabilities_default": True, "version": __version__,
                              "promptset": PROMPTSET_VERSION})


@routes.get("/api/sandbox")
async def sandbox_info(_: web.Request) -> web.Response:
    """How model-written code of the long sessions is isolated on this machine."""
    return web.json_response({"description": sandbox.description()})


@routes.post("/api/prompts")
async def save_prompt(request: web.Request) -> web.Response:
    body = await request.json()
    pid, text = str(body.get("id", "")).strip(), str(body.get("text", "")).strip()
    if not pid or not text:
        return error("ID and text are required.")
    if not _ID_RE.fullmatch(pid):
        return error("The ID may only have letters, digits, _ . - (up to 64 characters): it ends up in addresses and selectors.")
    prompts = all_prompts()
    existing = next((p for p in prompts if p.id == pid), None)
    if existing and existing.builtin:
        return error("Built-in prompt - please use a new ID.")
    mode = str(body.get("check", ""))
    if mode not in CHECK_MODES:
        return error(f"Unknown check mode '{mode}'.")
    lang = str(body.get("lang", ""))
    if lang and lang not in LANGUAGES:
        return error(f"Unknown language '{lang}'.")
    difficulty = str(body.get("difficulty") or "easy")
    if difficulty not in DIFFICULTIES:
        return error(f"Unknown difficulty '{difficulty}'.")
    # tools and turns are not edited in the form: keep those of the prompt it was opened from
    base = existing or next((p for p in prompts if p.id == body.get("base")), None)
    images = body["images"] if isinstance(body.get("images"), list) else (base.images if base else [])
    if missing := [str(n) for n in images if not image_exists(str(n))]:
        return error(f"Unknown image(s): {', '.join(missing)} - upload them again.")
    new = Prompt(pid, str(body.get("name", "")).strip() or pid, str(body.get("category", "")).strip() or "custom", text,
                 expected=str(body.get("expected", "")).strip(), check=mode,
                 images=[str(n) for n in images], tools=base.tools if base else [], turns=base.turns if base else [],
                 session=base.session if base else {}, lang=lang, difficulty=difficulty)
    with edit_custom_prompts() as custom:
        custom[:] = [p for p in custom if p.id != pid] + [new]
    storage.invalidate_list_cache()  # a changed expected answer/check changes stored runs' check scores
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- export / import of custom prompts
PROMPTS_FORMAT = "llm-probe-prompts"
_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # ids end up in URLs and selectors
_ENTRY_FIELDS = ("id", "name", "category", "text", "expected", "check", "images", "tools", "turns", "session", "lang",
                 "difficulty", "provenance")  # what a custom prompt may carry (no builtin / adaptive)


@routes.get("/api/prompts/export")
async def export_prompts(request: web.Request) -> web.Response:
    """All custom (non built-in) prompts - or those named by `id` - as one JSON file; images uploaded for them are embedded, built-in images are
    only referenced by name."""
    chosen = set(request.query.getall("id", []))  # given: only these (the ticked ones of the Prompts list)
    custom = [p for p in all_prompts() if not p.builtin and (not chosen or p.id in chosen)]
    images = {}
    for p in custom:
        for name in p.images:
            path = upload_path(str(name))
            if path is not None:
                images[name] = base64.b64encode(path.read_bytes()).decode()
    body = {"format": PROMPTS_FORMAT, "version": 1, "exported": datetime.now().isoformat(timespec="seconds"),
            "promptset": PROMPTSET_VERSION, "program": __version__,
            "prompts": [{k: v for k, v in asdict(p).items() if k in _ENTRY_FIELDS} for p in custom], "images": images}
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return web.json_response(body, headers={"Content-Disposition": f'attachment; filename="llm-probe-prompts-{stamp}.json"'})


def _parse_import(data) -> tuple[list[dict], dict[str, bytes], list[str]]:
    """Validate an exported prompt file: (entries, embedded images, problems). An entry with a problem is left out
    and named in the problems, the others go on."""
    if not isinstance(data, dict) or data.get("format") != PROMPTS_FORMAT or not isinstance(data.get("prompts"), list):
        raise ValueError("This is not an LLM Probe prompt file.")
    images: dict[str, bytes] = {}
    for name, b64 in (data.get("images") or {}).items():
        try:
            images[str(name)] = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise ValueError(f"Image '{name}' in the file is damaged.") from None
    entries, problems, seen = [], [], set()
    for raw in data["prompts"]:
        pid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
        why = ("not a prompt" if not isinstance(raw, dict) else "unsafe or missing id (letters, digits, _ . - only, up to 64)"
               if not _ID_RE.fullmatch(pid) else "duplicate id in the file" if pid in seen
               else "no text" if not str(raw.get("text", "")).strip()
               else f"unknown check mode '{raw.get('check')}'" if str(raw.get("check") or "") not in CHECK_MODES
               else f"unknown language '{raw.get('lang')}'" if str(raw.get("lang") or "") and str(raw.get("lang")) not in LANGUAGES
               else f"unknown difficulty '{raw.get('difficulty')}'" if str(raw.get("difficulty") or "easy") not in DIFFICULTIES
               else "")
        if why:
            problems.append(f"{pid or '(no id)'}: {why}")
            continue
        seen.add(pid)
        entries.append({k: raw[k] for k in _ENTRY_FIELDS if k in raw})
    return entries, images, problems


@routes.post("/api/prompts/import/plan")
async def import_plan(request: web.Request) -> web.Response:
    """What an import would run into: the prompts of the file and which ids already exist (custom or built-in)."""
    try:
        entries, images, problems = _parse_import((await request.json()).get("data"))
    except ValueError as e:
        return error(str(e))
    builtin = {p.id for p in all_prompts() if p.builtin}
    custom = {p.id for p in load_custom_prompts()}
    return web.json_response({
        "count": len(entries), "images": len(images), "problems": problems, "existing_custom": len(custom),
        "prompts": [{"id": e["id"], "name": e.get("name") or e["id"]} for e in entries],
        "conflicts": [{"id": e["id"], "name": e.get("name") or e["id"], "with": "builtin" if e["id"] in builtin else "custom"}
                      for e in entries if e["id"] in builtin or e["id"] in custom]})


@routes.post("/api/prompts/import")
async def import_prompts(request: web.Request) -> web.Response:
    """Apply an import. mode 'add' keeps the existing custom prompts, 'replace' drops them all first. resolutions
    decides every id conflict (custom: overwrite / rename / skip, built-in: rename / skip); the server checks them
    again - a missing or not allowed one refuses the whole import, nothing is changed."""
    body = await request.json()
    try:
        entries, images, problems = _parse_import(body.get("data"))
    except ValueError as e:
        return error(str(e))
    mode, resolutions = body.get("mode", "add"), body.get("resolutions") or {}
    if mode not in ("add", "replace"):
        return error("mode must be 'add' or 'replace'.")
    builtin = {p.id for p in all_prompts() if p.builtin}
    existing = set() if mode == "replace" else {p.id for p in load_custom_prompts()}
    taken = builtin | {p.id for p in load_custom_prompts()} | {e["id"] for e in entries}
    plan = []  # (entry, action)
    for e in entries:
        kind = "builtin" if e["id"] in builtin else "custom" if e["id"] in existing else ""
        action = resolutions.get(e["id"]) if kind else "add"
        allowed = {"builtin": ("rename", "skip"), "custom": ("overwrite", "rename", "skip")}.get(kind, ("add",))
        if action not in allowed:
            return error(f"Prompt '{e['id']}' already exists ({kind or 'custom'}): choose {' / '.join(allowed)}.")
        plan.append((e, action))
    # images: stored under a safe name; a name that exists with other content gets a new one (save_image), and a
    # built-in image of that name is never shadowed
    name_map: dict[str, str] = {}
    wanted = {str(n) for e, _ in plan for n in e.get("images", [])}  # images nobody asks for are not stored (a file could carry many MB of them)
    try:
        for name, data in images.items():
            if name not in wanted:
                continue
            safe = Path(name).name
            asset = IMAGES_ASSETS / safe
            if asset.is_file() and asset.read_bytes() != data:
                safe = "imported_" + safe
            name_map[name] = save_image(safe, data) if not (asset.is_file() and asset.read_bytes() == data) else safe
    except ValueError as e:
        return error(str(e))
    result = {"added": [], "replaced": [], "renamed": [], "skipped": [], "problems": list(problems)}
    with edit_custom_prompts() as custom:
        if mode == "replace":
            custom[:] = []
        by_id = {p.id: p for p in custom}
        for e, action in plan:
            if action == "skip":
                result["skipped"].append(e["id"])
                continue
            imgs = [name_map.get(str(n), str(n)) for n in e.get("images", [])]
            if missing := [n for n in imgs if not image_exists(n)]:
                result["problems"].append(f"{e['id']}: image(s) {', '.join(missing)} not in the file or on this machine - not imported")
                continue
            new = dict(e, images=imgs)
            if action == "rename":
                n = 2
                while f"{e['id']}_{n}" in taken:
                    n += 1
                new["id"], new["name"] = f"{e['id']}_{n}", f"{e.get('name') or e['id']} ({n})"
                taken.add(new["id"])
                result["renamed"].append([e["id"], new["id"]])
            prompt = Prompt(id=new["id"], name=new.get("name") or new["id"], category=new.get("category") or "custom", text=str(new["text"]),
                            **{k: new[k] for k in ("expected", "check", "images", "tools", "turns", "session", "lang", "difficulty", "provenance") if k in new})
            if action == "overwrite":
                result["replaced"].append(prompt.id)
            elif action != "rename":
                result["added"].append(prompt.id)
            by_id[prompt.id] = prompt
        custom[:] = list(by_id.values())
    storage.invalidate_list_cache()  # expected answers / checks of custom prompts change stored runs' scores
    return web.json_response({"ok": True, **result})


@routes.get("/api/images/{name}")
async def get_image(request: web.Request) -> web.Response:
    """Image of a prompt (built-in or from data/images), e.g. for the Responses tab."""
    name = request.match_info["name"]
    names = {n for p in all_prompts() for n in p.images}
    # images of prompts, and uploads not yet saved with a prompt
    if not image_exists(name) or not (name in names or upload_path(name) is not None):
        return error("Unknown image.", 404)
    return web.FileResponse(image_path(name))


@routes.post("/api/images")
async def upload_image(request: web.Request) -> web.Response:
    """Image for a custom prompt (raw request body, file name in ?name=)."""
    try:
        name = save_image(request.query.get("name", ""), await request.read())
    except ValueError as e:
        return error(str(e))
    return web.json_response({"name": name})


@routes.delete("/api/prompts/{id}")
async def delete_prompt(request: web.Request) -> web.Response:
    pid = request.match_info["id"]
    prompts = all_prompts()
    p = next((p for p in prompts if p.id == pid), None)
    if not p or p.builtin:
        return error("Only custom prompts can be deleted.")
    with edit_custom_prompts() as custom:
        custom[:] = [x for x in custom if x.id != pid]
    storage.invalidate_list_cache()
    return web.json_response({"ok": True})


@routes.get("/api/runs")
async def runs(request: web.Request) -> web.Response:
    db = storage.connect()
    sweep_interrupted(request, db)
    caps = storage.results_capabilities(db)  # one pass for all runs, combined with each run's capability check below
    out, damaged = [], []
    for r in storage.list_runs(db):
        try:
            out.append(run_row(db, r, caps))
        except Exception:  # a run whose stored data cannot be read (e.g. a damaged import) must not make the whole list unusable
            damaged.append(r)
    for r in damaged:  # shown as "damaged" (so it can be found and deleted), with the keys the page expects
        stub = dict.fromkeys(out[0], None) if out else {}
        stub.update({k: r[k] for k in ("id", "started", "finished", "endpoint", "kind", "base_url", "model", "note", "gpu_info")},
                    status="damaged", label=f"#{r['id']} (damaged)", settings={}, contents={}, capable={}, capable_evidence={}, usable=False, mode="benchmark")
        out.append(stub)
    out.sort(key=lambda x: -x["id"])
    db.close()
    return web.json_response(out)


def run_row(db, r, caps) -> dict:
    stats = storage.run_list_stats(db, r)  # summary + thinking + contents, cached for finished runs
    tps, ttft, ttfa, peak, score = stats["summary"]
    th = stats["thinking"]
    settings = json.loads(r["settings"])
    pl = settings.get("placement")
    return ({k: r[k] for k in ("id", "started", "finished", "status", "endpoint", "kind", "base_url", "model",
                                  "note", "gpu_info")}
               | {"label": storage.run_label(r), "avg_tps": tps, "avg_ttft": ttft, "avg_ttfa": ttfa, "peak": peak,
                  "check_score": score, "version": settings.get("version"),
                  "promptset": settings.get("promptset"),
                  "capable": storage.run_capabilities(settings, caps.get(r["id"])),
                  "usable": storage.run_usable(db, r["id"], settings),
                  "problems": storage.run_problems(db, r["id"]) if r["status"] == "running" else 0,
                  "capable_evidence": storage.capability_evidence(settings, caps.get(r["id"]), th),
                  "placement": placement.label(pl),
                  "placement_warn": placement.needs_attention(pl),
                  "thinking": storage.thinking_label(settings, th, short=True),
                  "thinking_warn": storage.thinking_warn(settings, th),
                  "thinking_capable": storage.thinking_capable(settings, th),
                  # for tooltips
                  "thinking_detail": storage.thinking_label(settings, th),
                  "thinking_share": None if th is None else th["share"] if th["thought"] else 0.0,
                  "placement_detail": placement.params_label(pl), "context": storage.context_label(settings),
                  "mode": settings.get("mode", "benchmark"),
                  "contents": stats["contents"],
                  "scores": stats["scores"],
                  "capabilities": storage.capability_summary(settings)
                  if storage.is_capability_run(settings) else None,
                  "settings": {k: settings.get(k) for k in ("max_tokens", "temperature", "repetitions",
                                                             "concurrency_levels", "requests_per_level",
                                                             "request_timeout", "run_single", "run_concurrency",
                                                             "thinking", "warmup_s", "error")}
                  | {"prompts": len(settings.get("prompts") or []),
                     "num_ctx": (settings.get("context") or {}).get("num_ctx"),  # what was asked for (Ollama), None = not set
                     "model_info": settings.get("model_info")}})


@routes.get("/api/export")
async def export_runs(request: web.Request) -> web.Response:
    """Selected runs as a JSON file to download (see storage.export_runs)."""
    try:
        ids = [int(x) for x in request.query["runs"].split(",") if x.strip()]
    except (KeyError, ValueError):
        return error("Parameter runs (comma-separated run ids) is required.")
    db = storage.connect()
    data = storage.export_runs(db, ids)
    db.close()
    data = mask_hosts(request, data, data["runs"])
    name = f"llm-probe-runs-{data['exported'][:19].replace(':', '').replace('T', '-')}.json"
    return web.json_response(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@routes.post("/api/import")
async def import_runs(request: web.Request) -> web.Response:
    try:
        data = json.loads(await request.read())
    except ValueError:
        return error("The file is not valid JSON.")
    db = storage.connect()
    try:
        new, skipped = storage.import_runs(db, data)
    except ValueError as e:
        return error(str(e))
    finally:
        db.close()
    return web.json_response({"imported": new, "skipped": skipped})


@routes.get("/api/runs/version")
async def runs_version(request: web.Request) -> web.Response:
    """Cheap check whether the run list changed (also by command-line runs), for refreshing it."""
    db = storage.connect()
    sweep_interrupted(request, db)
    v = storage.runs_version(db)
    db.close()
    return web.json_response({"version": v})


@routes.get("/api/runs/{id:\\d+}")
async def run(request: web.Request) -> web.Response:
    db = storage.connect()
    sweep_interrupted(request, db)
    d = run_detail(db, int(request.match_info["id"]))
    db.close()
    if d and request.query.get("mask") == "1":  # for a printed report: no server addresses
        d = mask_hosts(request, d, [d["run"]])
    return web.json_response(d) if d else error("Unknown run.", 404)


@routes.patch("/api/runs/{id:\\d+}")
async def run_note(request: web.Request) -> web.Response:
    body = await request.json()
    db = storage.connect()
    db.execute("UPDATE runs SET note = ? WHERE id = ?", (str(body.get("note", "")), int(request.match_info["id"])))
    db.commit()
    db.close()
    return web.json_response({"ok": True})


@routes.delete("/api/runs/{id:\\d+}")
async def run_delete(request: web.Request) -> web.Response:
    run_id = int(request.match_info["id"])
    if request.app["bench"].running and request.app["bench"].run_id == run_id:
        return error("Run is still in progress.")
    db = storage.connect()
    storage.delete_run(db, run_id)
    db.close()
    return web.json_response({"ok": True})


LETTERS = "ABCDEFGHIJKLMNOP"  # compared runs (up to 16): A is the baseline
MAX_RESPONSE_RUNS = 8  # runs the Responses tab shows side by side


@routes.get("/api/responses")
async def show_responses(request: web.Request) -> web.Response:
    """Responses tab, 1 to MAX_RESPONSE_RUNS runs (?runs=11,12). Without ?prompt= it returns only each run's per-prompt status
    (which prompts ran, for the dropdown); with ?prompt=pid it returns every repetition of that one prompt (text,
    timing, cut-off, answer check). Split this way so the dropdown is light and the heavy response texts load per
    selected prompt."""
    try:
        ids = list(dict.fromkeys(int(x) for x in request.query["runs"].split(",") if x.strip()))
    except (KeyError, ValueError):
        return error("Parameter runs (comma-separated run ids) is required.")
    if not 1 <= len(ids) <= MAX_RESPONSE_RUNS:
        return error(f"Show the responses of 1 to {MAX_RESPONSE_RUNS} runs.")
    pid = request.query.get("prompt")
    db = storage.connect()
    try:
        rows = [storage.get_run(db, r) for r in ids]
        if any(r is None for r in rows):
            return error("Unknown run.", 404)
        per_run = ([{"reps": storage.responses_full(db, r, pid)} for r in ids] if pid else
                   [{"status": storage.single_prompt_status(db, r)} for r in ids])
        out = {"runs": [{k: r[k] for k in ("id", "endpoint", "model", "started")} for r in rows],
               "responses": per_run}
    finally:
        db.close()
    return web.json_response(out)


@routes.get("/api/compare")
async def compare(request: web.Request) -> web.Response:
    """Everything for comparing runs (?runs=11,12,13, the first is the baseline): their details, warnings,
    key figures and the stored responses with answer check and timing."""
    try:
        ids = list(dict.fromkeys(int(x) for x in request.query["runs"].split(",") if x.strip()))
    except (KeyError, ValueError):
        return error("Parameter runs (comma-separated run ids) is required.")
    if not 1 <= len(ids) <= len(LETTERS):
        return error(f"Compare 1 to {len(LETTERS)} runs.")
    db = storage.connect()
    details = [run_detail(db, r) for r in ids]
    if any(d is None for d in details):
        db.close()
        return error("Unknown run.", 404)
    labels = list(LETTERS[:len(ids)])
    settings = [d["run"]["settings"] for d in details]
    stats = [{s.prompt_id: s for s in storage.single_stats(db, r)} for r in ids]
    names = {p.id: p.name for p in all_prompts()}
    texts = [storage.responses(db, r) for r in ids]
    hash_errors, hash_notes = storage.prompt_hash_check(settings, labels, lambda p: names.get(p, p))
    out = {
        "labels": labels, "runs": details,
        "errors": hash_errors, "notes": hash_notes,
        "warnings": storage.compare_warnings(settings, [storage.thinking_stats(db, r) for r in ids], labels)
        + [w for w in [storage.prompt_token_mismatch(stats, labels, lambda p: names.get(p, p))] if w],
        "kpis": storage.compare_kpis(db, ids),
        "capabilities": storage.capability_matrix(settings) if any(s.get("report") for s in settings) else [],
        "common": storage.common_prompts(stats),
        "responses": [{"text": t, "meta": storage.response_meta(db, r),
                       "checks": {pid: asdict(c) for pid, c in storage.response_checks(db, r).items()}}
                      for r, t in zip(ids, texts)],
    }
    db.close()
    if request.query.get("mask") == "1":  # for a printed report: no server addresses
        out = mask_hosts(request, out, [d["run"] for d in details])
    return web.json_response(out)


@routes.get("/api/bench")
async def bench_state(request: web.Request) -> web.Response:
    def arg(name: str, default: int) -> int:
        try:
            return int(request.query.get(name, default))
        except ValueError:
            return default

    return web.json_response(request.app["bench"].snapshot(arg("since", 0), arg("out", -1), arg("think", 0),
                                                           arg("text", 0)))


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"


@routes.get("/api/bench/stream")
async def bench_stream(request: web.Request) -> web.StreamResponse:
    """Live output as server-sent events: every chunk of the model is sent as soon as it has arrived (events that pile
    up while the browser is busy are sent together, so nothing is delayed on purpose and nothing queues up forever)."""
    bench: BenchState = request.app["bench"]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = bench.watch()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), 15)
            except asyncio.TimeoutError:
                await resp.write(b": keep-alive\n\n")
                continue
            events = [event]
            while not q.empty():
                events.append(q.get_nowait())
            stop = None in events  # the server shuts down
            out: list[dict] = []
            for e in events[:events.index(None)] if stop else events:  # consecutive deltas become one
                if out and e["k"] == "delta" and out[-1]["k"] == "delta":
                    out[-1] = {"k": "delta", "think": out[-1]["think"] + e["think"], "text": out[-1]["text"] + e["text"]}
                else:
                    out.append(e)
            await resp.write(b"".join(map(sse, out)))
            if stop:
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        bench.watchers.discard(q)
    return resp


async def close_streams(app: web.Application) -> None:
    """Open live output streams would keep the server from stopping."""
    app["bench"].push(None)


SERVER_PORT = 0  # the port this web UI listens on, set by serve() (shown to the other UIs while testing)


def take_lock(ep: Endpoint, models: list[str], what: str, force) -> TestLock | None:
    """The test lock shared with command-line runs; None if another process runs a test (unless force: then the test
    runs without the lock)."""
    lock = TestLock()
    if lock.acquire(f"web UI on port {SERVER_PORT}" if SERVER_PORT else "web UI", ep.name, models, what):
        return lock
    return TestLock() if force else None


async def locked(ep: Endpoint, what: str) -> web.Response:
    """Another process runs a test: who, and whether a test of `ep` would affect it (overlap.py)."""
    h = holder() or {}
    level, reason = await overlap.assess(ep, load_config().endpoint(h.get("endpoint", "")))
    return web.json_response({"error": holder_text(h) if h else "Another LLM Probe instance is running a test.",
                              "locked": True, "holder": h, "overlap": level,
                              "question": overlap.question(level, reason, what)}, status=409)


@routes.post("/api/bench/start")
async def bench_start(request: web.Request) -> web.Response:
    state: BenchState = request.app["bench"]
    if state.running:
        return error("A test is already running.", 409)
    body = await request.json()
    cfg = load_config()
    ep = cfg.endpoint(body.get("endpoint", ""))
    models = [str(m) for m in body.get("models") or ([body["model"]] if body.get("model") else []) if m]
    ids = set(body.get("prompts") or [])
    prompts = [p for p in all_prompts() if p.id in ids]
    capabilities = True  # the capability check is mandatory: it is the baseline for fair, capability-gated scoring
    try:
        settings = settings_from_body(body.get("settings") or {})
        params = clean_model_params(body.get("model_params"))  # what the user set per model (remembered)
        used = clean_model_params(body.get("model_effective", body.get("model_params")))  # what the runs use (thinking narrowed to what a model accepts)
    except ValueError as e:
        return error(str(e))
    per_model = {}
    for m in models:
        try:
            if m in used:
                per_model[m] = settings_from_body((body.get("settings") or {}) | used[m])
        except ValueError as e:
            return error(f"{m}: {e}")
    if not ep or not models:
        return error("Select an endpoint and at least one model.")
    if (n := sum(len(batch_plan(ep, per_model.get(m, settings))) for m in models)) > MAX_BATCH_RUNS:
        return error(f"{n} runs (models x values) is more than the {MAX_BATCH_RUNS} a batch may have - use fewer values.")
    if prompts and not settings.run_single and not settings.run_concurrency and not any(p.session for p in prompts):
        return error("Enable at least one phase.")
    if (lock := take_lock(ep, models, "test", body.get("force"))) is None:
        return await locked(ep, "test")
    with edit_config() as cfg:  # remember the selection; the file may have changed meanwhile (command line)
        cfg.bench = settings
        cfg.model_params[ep.name] = params  # the Test tab shows them again next time
        if stored := cfg.endpoint(ep.name):
            stored.model, stored.models = models[0], models
    runner = BatchRunner(ep, models, prompts, settings, state.emit, capabilities, per_model)
    what = models[0] if len(models) == 1 else f"batch of {len(models)} models"
    sel = " · ".join(filter(None, [f"{len(prompts)} prompts" if prompts else "", "capabilities" if capabilities else ""]))
    state.start(runner, f"[b]Starting test[/] {ep.name} ({ep.kind}) · {Text(what).markup} · {sel}", lock)
    return web.json_response({"ok": True})


@routes.post("/api/bench/stop")
async def bench_stop(request: web.Request) -> web.Response:
    state: BenchState = request.app["bench"]
    if state.running:
        state.task.cancel()
    return web.json_response({"ok": True})


# ----------------------------------------------------------------------- App

def basic_auth(user: str, password: str):
    expected = f"{user}:{password}".encode()

    @web.middleware
    async def middleware(request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                given = base64.b64decode(header[6:], validate=True)
            except (binascii.Error, ValueError):
                given = b""
            if hmac.compare_digest(given, expected):
                return await handler(request)
        return web.Response(status=401, text="Authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="LLM Probe", charset="UTF-8"'})

    return middleware


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def host_name(host_header: str) -> str:
    """'localhost:8080' -> 'localhost', '[::1]:8080' -> '::1'."""
    h = host_header.strip().lower()
    return h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def make_guard(local: bool, allowed_hosts: tuple[str, ...] = ()):
    """Protects the web UI against other web pages in the user's browser (it often runs without a password on 127.0.0.1):
    * DNS rebinding: bound to this computer only, a request must be addressed to localhost / 127.0.0.1 / ::1 (or a host named by
      --allow-host); a page that made its own name point to 127.0.0.1 is refused.
    * cross-site requests: a request that changes something (POST, DELETE ...) must come from this very page - a foreign Origin,
      `Origin: null` or Sec-Fetch-Site other than same-origin / none is refused (otherwise any web page could add an endpoint, start
      tests or redirect an endpoint with its stored API key to another server).
    Scripts without browser headers (curl, tests) are not affected."""
    ok_hosts = LOOPBACK_HOSTS | {h.lower() for h in allowed_hosts}

    @web.middleware
    async def guard(request: web.Request, handler):
        host_header = request.headers.get("Host", "")
        if local and host_name(host_header) not in ok_hosts:
            return web.Response(status=403, text="Refused: this web UI answers only to localhost (see --allow-host).")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin, site = request.headers.get("Origin"), request.headers.get("Sec-Fetch-Site")
            same = origin is None or origin.split("://", 1)[-1].lower() == host_header.lower()
            fwd = request.headers.get("X-Forwarded-Host", "")  # behind a reverse proxy that keeps the page's host in this header
            if (origin is not None and not same and not (fwd and origin.split("://", 1)[-1].lower() == fwd.lower())) \
                    or site not in (None, "same-origin", "none"):
                return web.Response(status=403, text="Refused: cross-site request.")
        return await handler(request)

    return guard


async def security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if response.content_type == "text/html":  # the page: scripts and styles are its own (inline), nothing may be loaded from or sent to elsewhere
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@web.middleware
async def json_errors(request: web.Request, handler):
    try:
        return await handler(request)
    except json.JSONDecodeError:
        return error("Invalid JSON body.")


def make_app(user: str = "", password: str = "", local: bool = True, allowed_hosts: tuple[str, ...] = ()) -> web.Application:
    middlewares = [make_guard(local, allowed_hosts)] + ([basic_auth(user, password)] if password else [])
    # large enough for importing exported runs (stored responses can be several MB)
    app = web.Application(middlewares=middlewares + [json_errors], client_max_size=256 * 1024 * 1024)
    app["bench"] = BenchState()
    app.on_shutdown.append(close_streams)
    app.on_response_prepare.append(security_headers)
    app.add_routes(routes)
    return app


def free_port(host: str, port: int, tries: int = 50) -> int:
    """The port, or the next free one after it if it is in use."""
    for p in range(port, port + tries):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name == "posix":  # like the server itself: a port in TIME_WAIT counts as free
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"No free port between {port} and {port + tries - 1} on {host}.")


def is_llm_probe(host: str, port: int) -> bool:
    """Whether an LLM Probe web UI answers on the port (it may ask for a login)."""
    import urllib.error
    import urllib.request
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else '[::1]' if host == '::' else host}:{port}/api/runs/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return "version" in json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code == 401 and "LLM Probe" in e.headers.get("WWW-Authenticate", "")
    except (OSError, ValueError):
        return False


def serve(host: str, port: int, user: str = "", password: str = "", allowed_hosts: tuple[str, ...] = (), open_browser: bool = False) -> None:
    local = host in ("127.0.0.1", "localhost", "::1")
    if password:
        print(f"Basic auth enabled (user '{user}').", flush=True)
    elif not local:
        print(f"WARNING: no password and listening on {host} - everyone who can reach this address can start tests, "
              "read all results and change the endpoints. "
              "Set --password (or LLM_PROBE_PASSWORD), or use --host 127.0.0.1.", flush=True)
    # local + no password: not a warning - the startup line below states it is local and unprotected
    global SERVER_PORT
    all_ifaces = host in ("0.0.0.0", "::")  # bound to every interface, not just this machine
    shown = "localhost" if all_ifaces else host  # a URL to click on this machine
    actual = SERVER_PORT = free_port(host, port)
    if actual != port:
        other = (f" by another LLM Probe web UI (http://{shown}:{port}/) - is it still needed?"
                 if is_llm_probe(host, port) else "")
        print(f"Note: port {port} is in use{other} Using port {actual} instead." if other else
              f"Note: port {port} is in use - using port {actual} instead.", flush=True)
    # spell out reachability on the startup line itself, so "127.0.0.1" is not mistaken for a network exposure
    if all_ifaces:
        note = f" - reachable from the network (bound to {host}:{actual})"
    elif local:
        note = " - only reachable from this computer" + ("" if password else ", no password")
    else:
        note = ""
    app = make_app(user, password, local, allowed_hosts)
    if open_browser:  # from here, not from the start script: the browser opens when the server really listens, on the port it really got
        async def open_page(_: web.Application) -> None:
            asyncio.get_running_loop().call_later(0.5, lambda: asyncio.get_running_loop().run_in_executor(None, webbrowser.open, f"http://{shown}:{actual}/"))
        app.on_startup.append(open_page)
    web.run_app(app, host=host, port=actual, access_log=None,
                print=lambda _: print(f"Web UI on http://{shown}:{actual}/{note} (Ctrl+C to stop)", flush=True))
