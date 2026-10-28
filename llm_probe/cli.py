"""Command line: start the web UI (default), run a test without a browser (--run), edit endpoints (--configure)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from dataclasses import asdict, replace

from rich.errors import MarkupError
from rich.text import Text

from . import __version__
from .bench import BatchRunner, clean_comment, result_line
from .client import api_error_hint, list_models
from .config import (DEFAULT_PORTS, clean_currency, MAX_BATCH_RUNS, THINKING_MODES, BenchSettings, Config, Endpoint, batch_plan, edit_config, load_config,
                     parse_values, split_values, valid_thinking)
from .lock import TestLock, holder, holder_text
from .prompts import DEFAULT_SELECTION, NO_LANGUAGE, Prompt, all_prompts, selection_facets

ENDPOINT_TYPES = ("ollama", "vllm", "openwebui", "openai")
FACETS = ("lang", "difficulty", "group", "kind")
# exit codes
EXIT_FAILED, EXIT_USAGE, EXIT_BUSY, EXIT_INTERRUPTED = 1, 2, 3, 130


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llm-probe", description="Benchmark and compare LLM endpoints. Without a mode option the web UI starts.",
        epilog="Passwords and API keys are never taken as arguments (they would show up in the process list): "
               "--set-api-key / --set-ssh-password read the environment variable LLM_PROBE_API_KEY / "
               "LLM_PROBE_SSH_PASSWORD, or ask on the terminal.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    web = p.add_argument_group("web UI (default)")
    web.add_argument("--server", action="store_true",
                     help="start the web UI (the default; kept so old scripts still work - it no longer opens the "
                          "network, use --host 0.0.0.0 for that)")
    web.add_argument("--host", default="127.0.0.1",
                     help="address to listen on (default: 127.0.0.1 = this computer only; 0.0.0.0 = the whole "
                          "network - then set --password)")
    web.add_argument("--port", type=int, default=8080, help="port to listen on (default: 8080)")
    web.add_argument("--open-browser", action="store_true",
                     help="open the web UI in the browser as soon as the server listens (used by the double-click start scripts)")
    web.add_argument("--user", default=os.environ.get("LLM_PROBE_USER", "admin"),
                     help="basic auth user (default: admin, env LLM_PROBE_USER)")
    web.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                     help="a host name the local web UI may be addressed by besides localhost / 127.0.0.1 (e.g. behind an SSH tunnel "
                          "or a reverse proxy); repeatable. Refusing other names protects against DNS rebinding.")
    web.add_argument("--password", default=os.environ.get("LLM_PROBE_PASSWORD", ""),
                     help="basic auth password; without one the web UI is open (env LLM_PROBE_PASSWORD)")

    mode = p.add_argument_group("batch mode (no browser)")
    modes = mode.add_mutually_exclusive_group()
    modes.add_argument("--run", nargs="+", metavar=("ENDPOINT", "MODEL"),
                       help="test one or more models of an endpoint and store each as a run; without a model the "
                            "last selection of the endpoint is used")
    modes.add_argument("--configure", metavar="ENDPOINT", help="create or change an endpoint (options below)")
    modes.add_argument("--list-endpoints", action="store_true", help="show the configured endpoints")
    modes.add_argument("--list-models", metavar="ENDPOINT", help="show the models an endpoint offers")
    modes.add_argument("--list-prompts", action="store_true",
                       help="show the prompts (all, or those taken by --lang/--difficulty/--group/--kind)")

    run = p.add_argument_group("options of --run (default: the settings last used in the web UI)")
    run.add_argument("--temperature", help="a value, or several comma separated ones: a batch (one run per value and model)")
    run.add_argument("--max-tokens", help="a value, or several comma separated ones: a batch (one run per value and model)")
    run.add_argument("--num-ctx-values", dest="num_ctx_values", metavar="N[,N...]",
                     help="Ollama: context lengths to load the model with, one run per value (a batch)")
    run.add_argument("--thinking", metavar="MODE[,MODE...]",
                     help="reasoning switch sent with every request: " + ", ".join(["default"] + [m for m in THINKING_MODES if m]) +
                     " (default = nothing sent); several comma separated ones: a batch")
    run.add_argument("--escape-word", choices=["unsolvable", "random"],
                     help="the word the hard prompts offer for 'I cannot answer this': unsolvable (default) or a random "
                     "code word per run (a model cannot know it from training; stored with the run)")
    run.add_argument("--reps", type=int, help="repetitions of every prompt")
    run.add_argument("--timeout", help="time limit of one request in seconds; several comma separated values: a batch (one run per value and model)")
    run.add_argument("--concurrency", metavar="LEVELS", help="load phase with these levels, e.g. 1,2,4,8")
    run.add_argument("--requests-per-level", type=int)
    run.add_argument("--no-single", action="store_true", help="skip the single-request phase")
    run.add_argument("--no-concurrency", action="store_true", help="skip the load phase")
    run.add_argument("--capabilities", action="store_true", help="(implied) the capability check always runs now")
    run.add_argument("--capabilities-only", action="store_true", help="run only the capability check, no prompts")
    run.add_argument("--comment", help="your words for the run(s); stored as the start of the run's note (e.g. a build or commit id)")
    run.add_argument("--force", action="store_true", help="start even if another LLM Probe runs a test")

    sel = p.add_argument_group("prompt selection (--run, --list-prompts); comma separated, 'none' = prompts "
                               "without a language")
    sel.add_argument("--lang", help=f"default: {','.join(x or 'none' for x in DEFAULT_SELECTION['lang'])}")
    sel.add_argument("--difficulty", help=f"default: {','.join(DEFAULT_SELECTION['difficulty'])}")
    sel.add_argument("--group", help=f"default: {','.join(DEFAULT_SELECTION['group'])}")
    sel.add_argument("--kind", help=f"default: {','.join(DEFAULT_SELECTION['kind'])}; also: safety, deviation")
    sel.add_argument("--prompts", metavar="IDS", help="exactly these prompt ids (see --list-prompts)")

    conf = p.add_argument_group("options of --configure (only the given ones change; a new endpoint needs --url)")
    conf.add_argument("--type", choices=ENDPOINT_TYPES, help="default for a new endpoint: guessed from the port")
    conf.add_argument("--url", help="base URL, e.g. http://host:11434")
    conf.add_argument("--model", help="model to test by default")
    conf.add_argument("--set-api-key", action="store_true", help="set the API key (see below how it is read)")
    conf.add_argument("--set-ssh-password", action="store_true", help="set the SSH password (see below)")
    conf.add_argument("--clear-secrets", action="store_true", help="remove the API key and the SSH password")
    conf.add_argument("--ssh-host")
    conf.add_argument("--ssh-port", type=int)
    conf.add_argument("--ssh-user")
    conf.add_argument("--ssh-key-file")
    conf.add_argument("--num-ctx", type=int, help="Ollama: context length to load the model with (0 = server default)")
    conf.add_argument("--price-in", type=float, help="price per 1 million input tokens")
    conf.add_argument("--price-out", type=float, help="price per 1 million output tokens")
    conf.add_argument("--currency", help="unit of the prices, e.g. EUR (shown next to the prices and the costs)")
    conf.add_argument("--delete", action="store_true", help="remove the endpoint")
    return p


def plain(markup: str) -> str:
    try:
        return Text.from_markup(markup).plain
    except MarkupError:  # e.g. brackets in an error message
        return markup


def fail(msg: str, code: int = EXIT_USAGE) -> int:
    print(f"llm-probe: {msg}", file=sys.stderr)
    return code


def find_endpoint(cfg: Config, name: str) -> Endpoint | None:
    ep = cfg.endpoint(name)
    if ep is None:
        known = ", ".join(e.name for e in cfg.endpoints) or "none configured"
        fail(f"unknown endpoint '{name}' (known: {known}). Create it with --configure.")
    return ep


# ------------------------------------------------------------------ selection

def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def facet_selection(args: argparse.Namespace, prompts: list[Prompt]) -> dict[str, list[str]] | str:
    """The facet selection given on the command line on top of DEFAULT_SELECTION, or an error message."""
    allowed = {f: [v for v, _ in vals] for f, vals in selection_facets(prompts).items()}
    sel = {f: list(v) for f, v in DEFAULT_SELECTION.items()}
    for f in FACETS:
        given = _csv(getattr(args, f))
        if not given:
            continue
        values = [NO_LANGUAGE if v == "none" and f == "lang" else v for v in given]
        bad = [v for v in values if v not in allowed[f]]
        if bad:
            return f"--{f}: unknown value {', '.join(map(repr, bad))} (allowed: {', '.join(x or 'none' for x in allowed[f])})"
        sel[f] = values
    return sel


def select_prompts(args: argparse.Namespace) -> list[Prompt] | str:
    prompts = all_prompts()
    if args.prompts:
        ids = _csv(args.prompts)
        known = {p.id for p in prompts}
        if unknown := [i for i in ids if i not in known]:
            return f"--prompts: unknown id {', '.join(map(repr, unknown))} (see --list-prompts)"
        return [p for p in prompts if p.id in ids]
    sel = facet_selection(args, prompts)
    if isinstance(sel, str):
        return sel
    return [p for p in prompts if p.matches(sel)]


# ------------------------------------------------------------------ list modes

def list_endpoints() -> int:
    cfg = load_config()
    if not cfg.endpoints:
        print("No endpoints configured. Create one with: llm-probe --configure NAME --url http://host:port")
    for e in cfg.endpoints:
        ssh = f", monitoring via ssh {e.ssh_user + '@' if e.ssh_user else ''}{e.ssh_host}" if e.ssh_host else ""
        last = f", last models: {', '.join(e.selected_models)}" if e.selected_models else ""
        print(f"{e.name}  [{e.kind}]  {e.base_url}{ssh}{last}")
    return 0


def list_endpoint_models(name: str) -> int:
    ep = find_endpoint(load_config(), name)
    if ep is None:
        return EXIT_USAGE
    try:
        models = asyncio.run(list_models(ep))
    except Exception as ex:
        return fail(f"API not reachable: {type(ex).__name__}: {ex}\n{api_error_hint(ex)}", EXIT_FAILED)
    print("\n".join(models) or "(the endpoint lists no models)")
    return 0


def list_prompt_table(args: argparse.Namespace) -> int:
    filtered = any(getattr(args, f) for f in FACETS)
    prompts = select_prompts(args) if filtered else all_prompts()
    if isinstance(prompts, str):
        return fail(prompts)
    for p in prompts:
        print(f"{p.id:32} {p.lang or '-':3} {p.difficulty:5} {p.kind:9} {p.group:11} {p.name}")
    print(f"{len(prompts)} prompts", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ run

def settings_from_args(args: argparse.Namespace, base: BenchSettings) -> BenchSettings | str:
    s = replace(base)
    # a setting with several comma separated values is a batch (config.batch_plan); the lists replace the saved ones
    s.max_tokens_values = s.temperature_values = s.thinking_values = s.num_ctx_values = s.request_timeout_values = []
    try:
        if args.temperature is not None:
            vals = parse_values(args.temperature, float, "--temperature", 0)
            if vals:
                s.temperature, s.temperature_values = vals[0], vals if len(vals) > 1 else []
        if args.max_tokens is not None:
            vals = parse_values(args.max_tokens, int, "--max-tokens", 1)
            if vals:
                s.max_tokens, s.max_tokens_values = vals[0], vals if len(vals) > 1 else []
        if getattr(args, "num_ctx_values", None):
            vals = parse_values(args.num_ctx_values, int, "--num-ctx-values", 128)
            s.num_ctx_values = vals
            if len(vals) == 1:
                return "--num-ctx-values needs several values; for one use --configure ENDPOINT --num-ctx N"
    except ValueError as e:
        return str(e)
    if args.thinking:
        modes = ["" if m == "default" else m for m in split_values(args.thinking)]
        bad = [m for m in modes if not valid_thinking(m)]
        if bad:
            return (f"--thinking: {bad[0]!r} is not a valid mode (letters, digits, - and _; the usual ones: "
                    f"{', '.join(['default'] + [m for m in THINKING_MODES if m])}; the Q button of the Test tab finds a model's own)")
        s.thinking, s.thinking_values = modes[0], modes if len(modes) > 1 else []
    if args.escape_word:
        s.escape_word = "random" if args.escape_word == "random" else ""
    if args.reps is not None:
        s.repetitions = max(1, args.reps)
    if args.timeout is not None:
        try:
            vals = parse_values(args.timeout, float, "--timeout", 1)
        except ValueError as e:
            return str(e)
        if vals:
            s.request_timeout, s.request_timeout_values = vals[0], vals if len(vals) > 1 else []
    if args.requests_per_level is not None:
        s.requests_per_level = max(1, args.requests_per_level)
    if args.concurrency:
        levels = sorted({int(x) for x in _csv(args.concurrency) if x.isdigit() and int(x) > 0})
        if not levels:
            return "--concurrency needs positive whole numbers, e.g. 1,2,4,8"
        s.concurrency_levels, s.run_concurrency = levels, True
    if args.no_single:
        s.run_single = False
    if args.no_concurrency:
        s.run_concurrency = False
    return s


def describe(s: BenchSettings) -> str:
    phases = [n for n, on in (("single", s.run_single), ("load " + ",".join(map(str, s.concurrency_levels)),
                                                         s.run_concurrency)) if on]
    return (f"max_tokens {','.join(map(str, s.max_tokens_values)) or s.max_tokens} · "
            f"temperature {','.join(f'{t:g}' for t in s.temperature_values) or s.temperature} · "
            f"thinking {','.join(m or 'default' for m in s.thinking_values) or s.thinking or 'default'}"
            f"{' · num_ctx ' + ','.join(map(str, s.num_ctx_values)) if s.num_ctx_values else ''}"
            f"{' · random escape word' if s.escape_word == 'random' else ''} · "
            f"{s.repetitions} reps · timeout {','.join(f'{t:g}' for t in s.request_timeout_values) or f'{s.request_timeout:g}'} s · phases: {', '.join(phases) or 'none'}")


def run_batch(args: argparse.Namespace) -> int:
    cfg = load_config()
    name, *models = args.run
    ep = find_endpoint(cfg, name)
    if ep is None:
        return EXIT_USAGE
    models = models or ep.selected_models
    if not models:
        return fail(f"no model given and none remembered for '{name}': llm-probe --run {name} MODEL")
    only_caps = args.capabilities_only
    prompts = [] if only_caps else select_prompts(args)
    if isinstance(prompts, str):
        return fail(prompts)
    settings = settings_from_args(args, cfg.bench)
    if isinstance(settings, str):
        return fail(settings)
    n_runs = len(models) * len(batch_plan(ep, settings))
    if n_runs > MAX_BATCH_RUNS:
        return fail(f"{n_runs} runs (models x values) is more than the {MAX_BATCH_RUNS} a batch may have - use fewer values")
    caps = True  # the capability check is mandatory on every test (the baseline for fair, capability-gated scoring);
    # --capabilities-only still runs it without any prompts, --capabilities is now implied
    if not prompts and not only_caps:
        return fail("the selection contains no prompt (see --list-prompts)")
    if prompts and not settings.run_single and not settings.run_concurrency and not any(p.session for p in prompts):
        return fail("no phase left to run (--no-single and --no-concurrency)")

    lock = TestLock()
    if not lock.acquire("command line", ep.name, models, "test"):
        if not args.force:
            return fail(f"{holder_text(holder() or {})} Use --force to start anyway.", EXIT_BUSY)
        lock = TestLock()  # runs without the lock

    print(f"LLM Probe {__version__} · {ep.name} ({ep.kind}) · {', '.join(models)} · "
          f"{len(prompts)} prompts{' + capability check' if caps else ''}")
    print(describe(settings) + (f" · {n_runs} runs" if n_runs > 1 else ""), flush=True)
    statuses: list[str] = []

    def emit(kind: str, **d) -> None:
        if kind == "log":
            print(plain(d["msg"]), flush=True)
        elif kind == "result":
            print(plain(result_line(d["prompt"], d["rep"], d["result"], d.get("word", ""))), flush=True)
        elif kind == "finished":
            statuses.append(d["status"])
            print(f"Run #{d['run_id']} finished: {d['status']}", flush=True)

    try:
        asyncio.run(BatchRunner(ep, models, prompts, settings, emit, caps, comment=clean_comment(args.comment)).run())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        lock.release()
    return 0 if statuses and all(s == "done" for s in statuses) else EXIT_FAILED


# ------------------------------------------------------------------ configure

def read_secret(env: str, label: str) -> str | None:
    if value := os.environ.get(env):
        return value
    if sys.stdin.isatty():
        return getpass.getpass(f"{label}: ")
    return None


def guess_type(url: str) -> str:
    for kind, port in DEFAULT_PORTS.items():
        if f":{port}" in url:
            return kind
    return "openai"


def configure(args: argparse.Namespace) -> int:
    name = args.configure.strip()
    if not name:
        return fail("the endpoint needs a name")
    secrets: dict[str, str] = {}
    for flag, env, label, field in ((args.set_api_key, "LLM_PROBE_API_KEY", "API key", "api_key"),
                                    (args.set_ssh_password, "LLM_PROBE_SSH_PASSWORD", "SSH password", "ssh_password")):
        if flag:
            value = read_secret(env, label)
            if value is None:
                return fail(f"{label}: set {env} or run in a terminal to be asked")
            secrets[field] = value
    with edit_config() as cfg:
        ep = cfg.endpoint(name)
        if args.delete:
            if ep is None:
                return fail(f"unknown endpoint '{name}'")
            cfg.endpoints.remove(ep)
            print(f"Endpoint '{name}' removed.")
            return 0
        if ep is None:
            if not args.url:
                return fail(f"'{name}' does not exist yet - give --url to create it")
            ep = Endpoint(name=name, kind=args.type or guess_type(args.url), base_url=args.url.strip())
            cfg.endpoints.append(ep)
            print(f"Endpoint '{name}' created (type {ep.kind}).")
        for field, value in (("kind", args.type), ("base_url", args.url and args.url.strip()), ("model", args.model),
                             ("ssh_host", args.ssh_host), ("ssh_port", args.ssh_port), ("ssh_user", args.ssh_user),
                             ("ssh_key_file", args.ssh_key_file), ("num_ctx", args.num_ctx),
                             ("price_in", args.price_in), ("price_out", args.price_out),
                             ("currency", None if args.currency is None else clean_currency(args.currency))):
            if value is not None:
                setattr(ep, field, value)
        if args.model is not None:
            ep.models = [args.model] if args.model else []  # the web UI keeps its model selection here
        if args.clear_secrets:
            ep.api_key = ep.ssh_password = ""
        for field, value in secrets.items():
            setattr(ep, field, value)
        shown = {k: v for k, v in asdict(ep).items() if k not in ("api_key", "ssh_password")}
        shown["api_key"] = "set" if ep.api_key else ""
        shown["ssh_password"] = "set" if ep.ssh_password else ""
    for k, v in shown.items():
        print(f"  {k}: {v}")
    return 0


# ------------------------------------------------------------------ entry point

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run is not None:
        return run_batch(args)
    if args.configure:
        return configure(args)
    if args.list_endpoints:
        return list_endpoints()
    if args.list_models:
        return list_endpoint_models(args.list_models)
    if args.list_prompts:
        return list_prompt_table(args)
    from .server import serve  # imported lazily: the batch modes do not need the web server

    serve(args.host, args.port, args.user, args.password, tuple(args.allow_host), args.open_browser)
    return 0
