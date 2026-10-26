"""Benchmark flow: warmup -> single requests -> concurrent load, with resource monitoring."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import asdict, replace
from datetime import datetime
from itertools import cycle
from typing import Callable

import httpx
from rich.text import Text

from .client import (RequestResult, chat_stream, context_info, model_capabilities, model_details, setting_rejected,
                     thinking_unsupported, unload_model, unsupported_capability)
from .config import BenchSettings, Endpoint, batch_plan, clean_currency
from .monitor import ResourceMonitor
from .check import ESCAPE_WORD, strip_thinking
from .deviation import TASKS, evaluate_step, extract_code
from .capcheck import CAP_STEPS, CapabilityProbe
from .prompts import (FIT_CAP_TOKENS, FIT_MIN_TOKENS, FIT_SHARE, OVERFLOW_MAX_CONTEXT, OVERFLOW_SHARE, PROMPTSET_VERSION,
                      SAFETY_MIN_TOKENS, OVERFLOW_ONE_REP_ABOVE, SWEEP_PASS, SWEEP_SIZES, SWEEP_STOP_AFTER, Prompt, build_adaptive, image_b64, image_mime, probe_log, prompt_hash, run_order, sweep_sizes, new_code_word, with_code_word)
from . import __version__, placement, sandbox, storage

# 32x32 grey PNG: probes whether a server accepts images at all
_PROBE_PNG = ("iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR42u3NQQEAAAQEMCS/6Erw2wqsk9SnqWcCgUAgEAgEAoHgygK+"
              "PQHAs0oJIwAAAABJRU5ErkJggg==")
_PROBE_TOOL = [{"type": "function", "function": {"name": "ping", "description": "Answer a ping.",
                                                  "parameters": {"type": "object", "properties": {}}}}]

EventCallback = Callable[..., None]


def result_line(p: Prompt, rep: int, r: RequestResult, word: str = "") -> str:
    """One log line (Rich markup) for a finished single request: timing, tokens and the answer check."""
    if not r.ok:
        return f"  [{rep + 1}] {p.name}: [yellow]{Text(r.error).markup}[/]"  # one failed request does not invalidate the others
    est = "" if r.tokens_from_usage else " (estimated)"
    tps = "-" if r.decode_tps is None else f"{r.decode_tps:.1f}"
    verdict = ""
    if c := p.evaluate(r.text, r.tool_calls, word=word):
        style = "green" if c.correct == c.total else "red"  # correctness only
        verdict = f" · check [{style}]{c.summary()}[/]"
    if r.text.startswith("[rejected]"):  # the overflow test: the server refused the too long prompt (no timing figures)
        return f"  [{rep + 1}] {p.name}: [green]the server rejected the prompt[/] ({Text(r.text[11:90]).markup}){verdict}"
    cut = {"tokens": " [yellow](cut off at max_tokens)[/]",
           "time": " [yellow](cut off at the time limit)[/]"}.get(r.limit, "")
    return (f"  [{rep + 1}] {p.name}: TTFT {r.ttft:.2f} s{storage.ttfa_note(r.ttft, r.ttfa)} · {tps} tok/s · "
            f"{r.completion_tokens} tokens{est}{cut}{verdict}")


class LiveOutput:
    """Forwards every streamed chunk at once (stream events). The chunk has been timestamped before it gets here, so
    showing it cannot delay the measurement."""

    def __init__(self, emit: EventCallback) -> None:
        self.emit = emit

    def start(self, title: str) -> None:
        self.emit("stream_start", title=title)

    def delta(self, content: str, think: str) -> None:
        if content or think:
            self.emit("stream", text=content, think=think)


# an error message of a server that refuses a prompt larger than its context
_CONTEXT_ERROR = re.compile(r"context|too long|too large|exceed|maximum|n_ctx|token limit|max_model_len|input.*length", re.I)
# vLLM: "This model's maximum context length is 32768 tokens and your request has 2386 input tokens (30499 > 32768 - 2386)"
_ROOM_ERROR = re.compile(r"maximum context length is (\d+) tokens.{0,40}?has (\d+) input tokens", re.I | re.S)
_LIMIT_IN_ERROR = re.compile(r"(?:context length|max(?:imum)?[ _]model[ _]len|n_ctx|context window)\D{0,40}(\d{3,7})", re.I)


class SettingRejected(RuntimeError):
    """The endpoint refused a setting of the test (e.g. a thinking level): a batch run stops after this run."""


class BenchmarkRunner:
    def __init__(self, ep: Endpoint, model: str, prompts: list[Prompt], settings: BenchSettings, emit: EventCallback,
                 capabilities: bool = False, note: str = ""):
        self.note = note  # stored with the run, e.g. "batch 3/12 · max_tokens 8192 · thinking high"
        self.stop_batch = ""  # set when a setting was rejected: the reason
        self.ctx_limit: int | None = None  # context per request, for servers that refuse prompt + max_tokens above it
        self.clamped: dict = {}
        self.ep = ep
        self.model = model
        self.capabilities = capabilities  # run the capability probe as a phase (findings in settings["report"])
        self.sessions = [p for p in prompts if p.session]  # long sessions: a phase of their own, at the end
        # the escape word of this run: "unsolvable", or a random code word (then every prompt that mentions the escape
        # route is sent with it; the hashes of the prompts stay those of the catalog, so runs remain comparable)
        self.word = new_code_word() if settings.escape_word == "random" else ESCAPE_WORD
        self.hashes = {p.id: prompt_hash(p) for p in prompts}
        prompts = [replace(p, text=with_code_word(p.text, self.word)) for p in prompts]
        self.sweeps = [p for p in prompts if p.adaptive == "sweep"]  # the context sweep: a phase of its own
        self.prompts = run_order([p for p in prompts if not p.session and p.adaptive != "sweep"])  # cheap first
        self.s = settings
        self.emit = emit  # emit(kind, **data); kind in log | progress | result | sample
        self.monitor = ResourceMonitor(ep, settings.monitor_interval)
        self.done = 0
        self.total_steps = 1 + sum(len(TASKS[p.session["task"]].steps) for p in self.sessions) + (CAP_STEPS if capabilities else 0)
        if settings.run_single:
            self.total_steps += settings.repetitions * len(self.prompts)
        if settings.run_concurrency and self.prompts:
            self.total_steps += sum(c * settings.requests_per_level for c in settings.concurrency_levels)
        if any(p.adaptive for p in self.prompts + self.sweeps):
            self.total_steps += 2  # the two context probes
        if self.sweeps:
            self.total_steps += len(self.sweeps) * len(SWEEP_SIZES) * settings.repetitions  # at most; unused ones are paid back
        self.eff: int | None = None  # effective context per request and tokens per log line (see _measure_context)
        self.tpl = 26.0
        self.adaptive: dict[str, dict] = {}  # prompts generated at run time -> what is stored with their responses
        self._phases: list[tuple[str, float]] = []  # (phase, monotonic start)
        self.live = LiveOutput(emit)
        self.kv_peak: dict[str, float] = {}  # vLLM: phase -> peak KV cache usage (0..1)
        self.thinking = settings.thinking  # dropped if the model has no reasoning

    def _log(self, msg: str) -> None:
        self.emit("log", msg=msg)

    @staticmethod
    def _colour(db, run_id: int, settings: dict) -> str:
        """Colour of an error or abort message: yellow if the run already has results to use (nothing gained is lost), red if not."""
        return "yellow" if storage.run_usable(db, run_id, settings) else "red"

    def _step(self) -> None:
        self.done += 1
        self.emit("progress", done=self.done, total=self.total_steps)

    def _phase(self, name: str) -> None:
        self._phases.append((name, time.monotonic()))
        self.emit("phase", name=name)

    def _phase_at(self, t: float) -> str:
        current = "warmup"
        for name, start in self._phases:
            if t >= start:
                current = name
        return current

    def _store(self, db, run_id: int, phase: str, prompt_id: str, conc: int, rep: int,
               r: RequestResult, wall: float | None = None, extra: dict | None = None) -> None:
        keep = phase in ("single", "session", "sweep")  # responses are kept, not those of the load phase
        if self.word != ESCAPE_WORD:
            extra = (extra or {}) | {"escape": self.word}  # read-time checks look for this word instead of "unsolvable"
        db.execute(
            "INSERT INTO results (run_id, phase, prompt_id, concurrency, rep, ok, ttft, ttfa, total, prompt_tokens,"
            " completion_tokens, decode_tps, e2e_tps, wall, text, error, tool_calls, extra, tokens_estimated, cut)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, phase, prompt_id, conc, rep, int(r.ok), r.ttft, r.ttfa, r.total, r.prompt_tokens, r.completion_tokens,
             r.decode_tps, r.e2e_tps, wall, r.text if keep else None, r.error,
             json.dumps(r.tool_calls, ensure_ascii=False) if keep and r.tool_calls else None,
             json.dumps(extra, ensure_ascii=False) if extra else None, int(r.ok and not r.tokens_from_usage),
             r.limit or None),
        )
        db.commit()

    def _live_title(self, what: str) -> str:
        """Title of the live output: the endpoint and model under test, then the request (several models run in a batch)."""
        return f"{self.ep.name} · {self.model} · {what}"

    def _fit_max_tokens(self, wanted: int, chars: int, images: int = 0, est_tokens: int | None = None) -> int:
        """vLLM and other OpenAI-compatible servers refuse a request whose prompt + max_tokens exceed the context (Ollama
        shifts its context instead). With the default max_tokens of 32768 on a model with a 32768 context EVERY request
        was refused, so on such servers max_tokens is cut to what the context leaves after the prompt (a safe estimate of
        its tokens; the generated prompts know theirs). Noted once in the log and in the run (settings["clamped"])."""
        ctx = self.ctx_limit
        if not ctx:
            return wanted
        # est_tokens counts the log lines only: the instructions around them and the chat template come on top
        est = (max(int(est_tokens * 1.08), int(chars / 2.6)) + 300) if est_tokens else int(chars / 2.2) + images * 4096 + 64
        room = ctx - est
        if room >= wanted:
            return wanted
        sent = max(256, room)
        clamp = self.clamped
        if not clamp:
            self._log(f"[yellow]max_tokens {wanted} does not fit the context of {ctx} tokens after the prompt - the server would "
                      f"refuse every request. Sending at most what is left (here {sent}) instead.[/]")
        self.clamped = {"asked": wanted, "context": ctx, "min_sent": min(sent, clamp["min_sent"]) if clamp else sent}
        return sent

    @staticmethod
    def _check_rejected(r: RequestResult) -> None:
        """After the response is stored: a refused setting stops the run (and a batch)."""
        if getattr(r, "rejected_setting", False):
            raise SettingRejected(f"the endpoint refused a setting: {r.error}")

    async def _request(self, client: httpx.AsyncClient, prompt: Prompt, rep: int = 0,
                       live: LiveOutput | None = None, est_tokens: int | None = None) -> RequestResult:
        # one seed per repetition: reproducible across runs, but repetitions are distinct samples at temperature > 0
        if live:
            live.start(self._live_title(f"[{rep + 1}] {prompt.name}"))
        # refusal tests: room for the reasoning before the answer, which decides the verdict
        max_tokens = max(self.s.max_tokens, SAFETY_MIN_TOKENS) if prompt.kind == "safety" else self.s.max_tokens
        if prompt.adaptive != "overflow":  # (that one is meant to be refused or cut: the size of the answer does not matter)
            max_tokens = self._fit_max_tokens(max_tokens, len(prompt.text) + sum(len(str(t)) for t in prompt.turns + prompt.tools),
                                              len(prompt.images), est_tokens or self.adaptive.get(prompt.id, {}).get("est_tokens"))
        ask = lambda mt: chat_stream(client, self.ep, self.model, prompt.text, mt, self.s.temperature,  # noqa: E731
                                     self.s.request_timeout, seed=1234 + rep, on_delta=live.delta if live else None,
                                     thinking=self.thinking, images=[(image_mime(n), image_b64(n)) for n in prompt.images],
                                     tools=prompt.tools, turns=prompt.turns)
        r = await ask(max_tokens)
        # the estimate was too low: the server says how much room is really left - use it, once
        m = _ROOM_ERROR.search(r.error or "") if not r.ok and self.ctx_limit and prompt.adaptive != "overflow" else None
        if m and (room := int(m.group(1)) - int(m.group(2)) - 8) >= 256 and room < max_tokens:
            self.clamped = {"asked": self.s.max_tokens, "context": self.ctx_limit, "min_sent": min(room, (self.clamped or {}).get("min_sent", room))}
            r = await ask(room)
        if prompt.adaptive == "overflow" and not r.ok and _CONTEXT_ERROR.search(r.error or ""):
            # the server refused the too long prompt explicitly: that is the honest outcome this test looks for, so it is
            # stored as a response (without timing or token figures) instead of an error
            r.ok, r.text, r.error = True, "[rejected] " + r.error, ""
        r.rejected_setting = not r.ok and setting_rejected(r.error)  # the caller stores the response first, then stops
        if live:
            if r.tool_calls:
                live.emit("stream", text="\n" + "\n".join(f"[tool call] {storage.call_label(c)}"
                                                           for c in r.tool_calls), think="")
        return r

    async def _warmup(self, client: httpx.AsyncClient) -> RequestResult:
        return await chat_stream(client, self.ep, self.model, "Say hello.", 16, 0.0, self.s.request_timeout,
                                 thinking=self.thinking)

    async def _capabilities(self, client: httpx.AsyncClient) -> dict:
        """Which of the capabilities the prompts need (vision, tools) the model has: as the server declares
        them, else by a tiny probe request. Returns settings["capabilities"]; drops the prompts it cannot run."""
        needed = sorted({c for p in self.prompts for c in p.needs})
        declared, source = await model_capabilities(self.ep, self.model)
        info: dict = {"declared": declared, "source": source, "needed": needed, "probed": {}, "skipped": {}}
        if not needed:
            return info
        missing = set()
        for cap in needed:
            if declared is not None:
                if cap not in declared:
                    missing.add(cap)
                continue
            r = await chat_stream(client, self.ep, self.model, "Reply with OK.", 8, 0.0, self.s.request_timeout,
                                  thinking=self.thinking, images=[("image/png", _PROBE_PNG)] if cap == "vision" else None,
                                  tools=_PROBE_TOOL if cap == "tools" else None)
            refused = not r.ok and unsupported_capability(r.error) == cap
            info["probed"][cap] = "refused" if refused else "accepted" if r.ok else f"unclear: {r.error[:120]}"
            if refused:
                missing.add(cap)
        keep = []
        for p in self.prompts:
            lacking = [c for c in p.needs if c in missing]
            if lacking:
                info["skipped"][p.id] = "no " + " / ".join(lacking)
            else:
                keep.append(p)
        if info["skipped"]:
            self.done += self.s.repetitions * len(info["skipped"]) if self.s.run_single else 0
            self._log(f"[yellow]Skipped {len(info['skipped'])} prompt(s) - the model has no "
                      f"{' / '.join(sorted(missing))}: {', '.join(p.name for p in self.prompts if p not in keep)}[/]")
            self.prompts = keep
        return info

    async def _measure_context(self, client: httpx.AsyncClient, ctx: dict) -> dict:
        """What a request really gets, measured with the same options as the real requests. Two small probes: a short
        log gives the tokens per log line of this model's tokenizer; a log of 1.5 x the declared context shows whether
        the server really has that context per request - it cuts the prompt (llama.cpp / Ollama keep about half of the
        context, which gives the context back as 2 x (kept - 2); a context split between parallel slots shows up as a
        smaller value than declared) or rejects it with an error that names the limit (vLLM)."""
        declared = ctx.get("context")
        info: dict = {"declared": declared, "tokens": declared, "tokens_per_line": 26.0, "source": "declared" if declared else "unknown"}

        async def ask(n_lines: int) -> RequestResult:
            r = await chat_stream(client, self.ep, self.model, probe_log(n_lines), 16, 0.0, self.s.request_timeout,
                                  thinking=self.thinking)
            self._step()
            return r
        n_a = max(8, min(120, int(0.2 * declared / 26))) if declared else 100
        r = await ask(n_a)
        if r.ok and r.prompt_tokens and 8 <= r.prompt_tokens / n_a <= 90:
            info["tokens_per_line"] = round(r.prompt_tokens / n_a, 2)
        else:
            self._log(f"[yellow]Context probe 1 gave no usable token count - assuming {info['tokens_per_line']} tokens per log line.[/]")
        if not declared or declared > OVERFLOW_MAX_CONTEXT - 4000:  # 1.5 x the context would be too much to send and process
            self._step()  # no second probe: the context is unknown or too large to fill cheaply
            info["source"] = "declared (not probed: " + ("unknown" if not declared else "too large") + ")"
            return info
        n_b = math.ceil(1.5 * declared / info["tokens_per_line"])
        r = await ask(n_b)
        expected = n_b * info["tokens_per_line"]
        if not r.ok:
            m = _LIMIT_IN_ERROR.search(r.error or "")
            if m:
                info["tokens"], info["source"] = int(m.group(1)), "server error"
            else:
                info["source"] = f"declared (probe failed: {(r.error or '')[:80]})"
        elif r.prompt_tokens and r.prompt_tokens < 0.85 * expected:
            # a llama.cpp based server (Ollama) that gets a prompt larger than its context keeps about HALF of the context
            # (+ 2 tokens): 16386 of a context of 32768, 8194 of 16384 (the cause of a series of failed runs on a local Ollama server), 4098 of 8192
            info["tokens"] = 2 * (r.prompt_tokens - 2)
            info["source"] = f"measured (the server kept {r.prompt_tokens} tokens of the probe prompt)"
        else:
            info["tokens"], info["source"] = max(declared, r.prompt_tokens or 0), "no cut up to " + str(r.prompt_tokens or "?")
        return info

    async def _prepare_adaptive(self, client: httpx.AsyncClient, settings: dict, ctx: dict) -> None:
        """Generate the prompts that follow the context (see prompts.build_adaptive) and remember what is stored with
        their responses: the expected answer, the size and the context they were made for."""
        info = await self._measure_context(client, ctx)
        eff, tpl = info["tokens"], info["tokens_per_line"]
        self.eff, self.tpl = eff, tpl
        settings.setdefault("context", {})["effective"] = {k: info[k] for k in ("declared", "tokens", "tokens_per_line", "source")}
        self._log(f"Effective context per request: [b]{eff or 'unknown'}[/] ({info['source']}), {tpl} tokens per log line"
                  + (f" - the server says {info['declared']}" if info["declared"] and info["declared"] != eff else ""))
        kept = []
        for p in self.prompts:
            if not p.adaptive:
                kept.append(p)
                continue
            if p.adaptive == "overflow" and (not eff or eff > OVERFLOW_MAX_CONTEXT):
                reason = "context unknown" if not eff else f"context of {eff} tokens is too large to overflow cheaply"
                settings.setdefault("capabilities", {}).setdefault("skipped", {})[p.id] = reason
                self.done += self.s.repetitions if self.s.run_single else 0
                self._log(f"[yellow]Skipped {p.name}: {reason}.[/]")
                continue
            target = (round(OVERFLOW_SHARE * eff) if p.adaptive == "overflow"
                      else max(FIT_MIN_TOKENS, min(FIT_CAP_TOKENS, round(FIT_SHARE * eff))) if eff else 6000)
            d = build_adaptive(p.adaptive, target, tpl)
            self.adaptive[p.id] = {k: d[k] for k in ("expected", "chars", "est_tokens", "lines", "incidents")} | {
                "kind": p.adaptive, "context": eff, "declared": info["declared"], "target_tokens": target}
            if p.adaptive == "overflow" and eff > OVERFLOW_ONE_REP_ABOVE:
                self.adaptive[p.id]["reps"] = 1
                self._log(f"  (the overflow test runs once: a context of {eff} tokens makes every request slow)")
            kept.append(replace(p, text=with_code_word(d["text"], self.word), expected=d["expected"]))
            self._log(f"  {p.name}: {d['lines']} log lines, about {d['est_tokens']} tokens "
                      f"({'130 %' if p.adaptive == 'overflow' else 'about 40 %'} of the context), {d['incidents']} critical incidents")
        self.prompts = kept
        settings["adaptive"] = {pid: {k: v for k, v in a.items() if k != "expected"} for pid, a in self.adaptive.items()}

    async def _sweep(self, client: httpx.AsyncClient, db, run_id: int, p: Prompt) -> None:
        """The context sweep: the log triage at growing sizes, every size as many times as the repetitions. A size
        passes at >= 2/3 correct; responses cut at max_tokens or by the server are not testable (neither pass nor
        fail); two failed sizes in a row end the sweep (the larger ones would only cost time)."""
        sizes = sweep_sizes(self.eff)
        reps = self.s.repetitions
        self.done += (len(SWEEP_SIZES) - len(sizes)) * reps  # progress: the sizes this context does not have
        label = lambda n: f"{n // 1024}k" if n >= 1024 and n % 1024 == 0 else str(n)  # noqa: E731
        self._log(f"[b]Context sweep:[/] {', '.join(label(s) for s in sizes)} tokens x {reps} repetitions "
                  f"(effective context {self.eff or 'unknown'})")
        failed_in_a_row = 0
        for i, size in enumerate(sizes):
            d = build_adaptive("fit", size, self.tpl)
            prompt = replace(p, text=with_code_word(d["text"], self.word), expected=d["expected"], name=f"{p.name} ({label(size)})")
            extra = {k: d[k] for k in ("expected", "chars", "est_tokens", "lines", "incidents")} | {
                "kind": "sweep", "size": size, "context": self.eff}
            scores = []
            for rep in range(reps):
                r = await self._request(client, prompt, rep, self.live, est_tokens=d["est_tokens"])
                self._store(db, run_id, "sweep", p.id, 1, rep, r, extra=extra)
                self.emit("result", phase="sweep", prompt=prompt, rep=rep, result=r, word=self.word)
                self._step()
                self._check_rejected(r)
                if r.ok and r.limit not in ("tokens", "time") and (c := prompt.evaluate(r.text, r.tool_calls, word=self.word)):
                    scores.append(c.score)
            mean = sum(scores) / len(scores) if scores else None
            if mean is None:
                self._log(f"  {label(size)}: [yellow]not testable[/] (every response was cut off)")
                continue
            ok = mean >= SWEEP_PASS - 1e-9
            self._log(f"  {label(size)}: [{'green' if ok else 'red'}]{mean * 100:.0f} % correct[/]")
            failed_in_a_row = 0 if ok else failed_in_a_row + 1
            if failed_in_a_row >= SWEEP_STOP_AFTER and i + 1 < len(sizes):
                self.done += (len(sizes) - i - 1) * reps
                self._log(f"  [yellow]{failed_in_a_row} sizes failed in a row - the sweep stops here.[/]")
                return

    async def _session(self, client: httpx.AsyncClient, db, run_id: int, p: Prompt) -> None:
        """Long session (deviation.py): the steps one after another in one growing conversation; the module of
        every step is tested in the sandbox."""
        task = TASKS[p.session["task"]]
        own = p.session.get("mode") == "own"
        history: list[dict] = []  # turns after the first request
        max_tokens = max(self.s.max_tokens, 4096)  # the whole module in every reply
        self._log(f"  {p.name}: {len(task.steps)} steps, max_tokens {max_tokens}")
        for n, step in enumerate(task.steps, 1):
            if n > 1:
                history.append({"role": "user", "content": step.request})
            if self.live:
                self.live.start(self._live_title(f"[step {n}/{len(task.steps)}] {p.name}"))
            chars = len(task.steps[0].request) + len(task.system or "") + sum(len(str(m.get("content", ""))) for m in history)
            r = await chat_stream(client, self.ep, self.model, task.steps[0].request, self._fit_max_tokens(max_tokens, chars),
                                  self.s.temperature, self.s.request_timeout, seed=1234, on_delta=self.live.delta if self.live else None,
                                  thinking=self.thinking, turns=list(history), system=task.system)
            if r.ok:
                ev = await asyncio.to_thread(evaluate_step, task, n, extract_code(r.text))
            else:
                ev = {"step": n, "error": r.error}
            ev["cut"] = r.limit == "tokens"
            self._store(db, run_id, "session", p.id, 1, n, r, extra=ev)
            self._step()
            if not r.ok:
                self._log(f"    step {n}: [yellow]{Text(r.error).markup}[/] - session stopped")
                self.done += len(task.steps) - n
                return
            earlier = f" · earlier {ev['earlier_ok']}/{ev['earlier_n']}" if ev["earlier_n"] else ""
            bad = ev["new_ok"] < ev["new_n"] or ev["earlier_ok"] < ev["earlier_n"] or ev["rules"] or ev["error"]
            new = f"new {ev['new_ok']}/{ev['new_n']}" if ev["new_n"] else "no new tests"
            tps = "-" if r.decode_tps is None else f"{r.decode_tps:.1f}"
            self._log(f"    step {n}: [{'yellow' if bad else 'green'}]{new}{earlier}"
                      f"{' · rules: ' + Text('; '.join(ev['rules'])).markup if ev['rules'] else ''}"
                      f"{' · ' + Text(ev['error']).markup if ev['error'] else ''}[/]"
                      f" · TTFT {r.ttft:.2f} s · {tps} tok/s · {r.completion_tokens or '?'} out / {r.prompt_tokens or '?'} prompt tok"
                      + (" [yellow](cut off at max_tokens)[/]" if ev["cut"] else ""))
            reply = strip_thinking(r.text) if own else f"```python\n{step.ref}```"
            history.append({"role": "assistant", "content": reply})

    async def _kv_usage(self) -> None:
        """vLLM: peak KV cache usage per phase from /metrics."""
        peak = self.kv_peak
        while True:
            try:
                _, usage = await placement.vllm_metrics(self.ep)
            except httpx.HTTPError:
                usage = None
            if usage is not None:
                phase = self._phase_at(time.monotonic())
                peak[phase] = max(peak.get(phase, 0.0), usage)
            await asyncio.sleep(self.s.monitor_interval)

    async def _live_samples(self) -> None:
        seen = 0
        while True:
            await asyncio.sleep(self.s.monitor_interval)
            if len(self.monitor.samples) > seen:
                seen = len(self.monitor.samples)
                self.emit("sample", sample=self.monitor.samples[-1])
            if self.monitor.error:
                self._log(f"[yellow]Monitoring error:[/] {self.monitor.error}")
                return

    async def run(self) -> int:
        db = storage.connect()
        # stamp the program version and the prompt-set version the run was executed under, so a report always shows
        # which version produced it and which wording the models were asked (responses only store the prompt id)
        settings = asdict(self.s) | {"version": __version__, "promptset": PROMPTSET_VERSION,
                                     "prompts": [p.id for p in self.prompts + self.sweeps + self.sessions],
                                     "prompt_hashes": {p.id: self.hashes[p.id] for p in self.prompts + self.sweeps + self.sessions}}
        if self.word != ESCAPE_WORD:
            settings["code_word"] = self.word  # the checks need it to tell a signal from an answer
        if self.ep.price_in or self.ep.price_out:  # stored with the run: later price changes keep old runs as they were
            settings["price"] = {"in": self.ep.price_in, "out": self.ep.price_out, "currency": clean_currency(self.ep.currency)}
        cur = db.execute(
            "INSERT INTO runs (started, status, endpoint, kind, base_url, model, settings, note) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), "running", self.ep.name, self.ep.kind,
             self.ep.base_url, self.model, json.dumps(settings), self.note),
        )
        db.commit()
        run_id = cur.lastrowid
        t_run = time.monotonic()
        status = "failed"
        live = kv = None
        try:
            if self.monitor.enabled:
                await self.monitor.start()
                live = asyncio.create_task(self._live_samples())
                self._log(f"Resource monitoring via SSH {self.ep.ssh_user + '@' if self.ep.ssh_user else ''}{self.ep.ssh_host}")
            else:
                self._log("[yellow]No SSH host configured - no resource monitoring.[/]")

            limits = httpx.Limits(max_connections=max(self.s.concurrency_levels + [1]) + 2)
            async with httpx.AsyncClient(limits=limits) as client:
                # Warmup: makes Ollama load the model into VRAM, not part of the measurement
                self._phase("warmup")
                self._log(f"Warmup ({self.model}) ...")
                t = time.monotonic()
                w = await self._warmup(client)
                if not w.ok and self.thinking and thinking_unsupported(w.error):
                    # a level for a model without reasoning: test it as it is (matters in a batch)
                    settings["thinking_note"] = "model has no reasoning, sent without"
                    self._log(f"[yellow]{self.model} has no reasoning - testing without the thinking setting.[/]")
                    self.thinking = ""
                    w = await self._warmup(client)
                if not w.ok:
                    if setting_rejected(w.error):
                        raise SettingRejected(f"the endpoint refused a setting at the warmup: {w.error}")
                    raise RuntimeError(f"Warmup failed: {w.error}")
                settings["warmup_s"] = round(time.monotonic() - t, 2)
                self._log(f"Warmup ok after {settings['warmup_s']} s")
                if self.thinking:
                    self._log(f"Thinking requested: {self.thinking}")
                settings["context"] = ctx = await context_info(self.ep, self.model)
                self.ctx_limit = None if self.ep.ollama_like else ctx.get("context")
                self._log(f"Context: {storage.context_label(settings)}")
                if ctx.get("error"):
                    self._log(f"[yellow]Could not query the context length:[/] {ctx['error']}")
                elif ctx["num_ctx"] and ctx["context"] and ctx["context"] != ctx["num_ctx"]:
                    self._log(f"[yellow]Requested num_ctx {ctx['num_ctx']}, but the model runs with {ctx['context']}.[/]")
                settings["capabilities"] = caps = await self._capabilities(client)
                if caps["needed"] or caps["declared"] is not None:
                    self._log(f"Capabilities: {storage.capability_label(settings)}")
                if not self.prompts and not self.sessions and not self.capabilities:
                    raise RuntimeError("The model supports none of the selected prompts.")
                settings["placement"] = pl = await placement.collect(self.ep, self.model)
                info = await model_details(self.ep, self.model)
                if quant := placement.params(pl).get("quant"):  # what the server was started with beats what the name says
                    info["quant"], info["quant_source"] = quant, "command line"
                settings["model_info"] = info
                self._log(f"Model: {storage.model_info_label(settings)}")
                text = placement.label(pl)
                style = "yellow" if placement.needs_attention(pl) else "green" if text != "-" else "dim"
                self._log(f"Placement: [{style}]{text}[/]")
                self._log(f"Server parameters: {placement.params_label(pl)}")
                for err in pl["errors"]:
                    self._log(f"[yellow]Placement check incomplete:[/] {err}")
                if pl.get("ssh", {}).get("note"):
                    self._log(f"[yellow]Placement via SSH:[/] {pl['ssh']['note']}")
                if ctx.get("others"):
                    self._log(f"[yellow]Also loaded on this server: {', '.join(ctx['others'])} - they share the "
                              "GPU memory and may slow this test down.[/]")
                self._step()
                if self.ep.kind == "vllm":
                    kv = asyncio.create_task(self._kv_usage())

                if self.capabilities:
                    self._phase("capabilities")
                    self._log("[b]Capabilities:[/] what the model can do and which settings the endpoint obeys")
                    probe = CapabilityProbe(self.ep, self.model, self.s, client, self.emit, self._step)
                    try:
                        settings["report"] = await probe.report()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # a failed probe must not abort the measurements
                        self._log(f"[yellow]Capability check incomplete:[/] {e}")

                if any(p.adaptive for p in self.prompts + self.sweeps):
                    await self._prepare_adaptive(client, settings, ctx)
                if self.s.run_single and self.prompts:
                    self._phase("single")
                    self._log("[b]Phase 1:[/] single requests (sequential)")
                    for rep in range(self.s.repetitions):
                        for p in self.prompts:
                            if rep >= self.adaptive.get(p.id, {}).get("reps", rep + 1):  # e.g. the overflow test of a huge context
                                self._step()
                                continue
                            r = await self._request(client, p, rep, self.live)
                            self._store(db, run_id, "single", p.id, 1, rep, r, extra=self.adaptive.get(p.id))
                            self.emit("result", phase="single", prompt=p, rep=rep, result=r, word=self.word)
                            self._step()
                            self._check_rejected(r)

                    th = storage.thinking_stats(db, run_id)
                    style = "yellow" if storage.thinking_warn(settings, th) else "green"
                    self._log(f"Thinking: [{style}]{storage.thinking_label(settings, th)}[/]")

                load_prompts = [p for p in self.prompts if not p.adaptive]  # the generated ones are far too large for a load test
                if self.s.run_concurrency and load_prompts:
                    self._phase("load")
                    self._log("[b]Phase 2:[/] concurrent load")
                    for c in self.s.concurrency_levels:
                        n = c * self.s.requests_per_level
                        self._log(f"  {c} concurrent, {n} requests ...")
                        sem = asyncio.Semaphore(c)
                        prompt_iter = cycle(load_prompts)
                        jobs = [next(prompt_iter) for _ in range(n)]

                        async def one(p: Prompt) -> tuple[Prompt, RequestResult]:
                            async with sem:
                                r = await self._request(client, p)
                                self._step()
                                return p, r

                        t0 = time.monotonic()
                        results = await asyncio.gather(*(one(p) for p in jobs))
                        wall = time.monotonic() - t0
                        tokens = 0
                        for i, (p, r) in enumerate(results):
                            self._store(db, run_id, "load", p.id, c, i, r, wall)
                            if r.ok:
                                tokens += r.completion_tokens or 0
                        for _, r in results:
                            self._check_rejected(r)
                        errors = sum(not r.ok for _, r in results)
                        self.emit("load_level", concurrency=c, throughput=tokens / wall, wall=wall, errors=errors)
                        self._log(f"  -> {tokens / wall:.1f} tok/s total in {wall:.1f} s"
                                  + (f", [yellow]{errors} errors[/]" if errors else ""))

                if self.sweeps and self.s.run_single:
                    self._phase("sweep")
                    for p in self.sweeps:
                        await self._sweep(client, db, run_id, p)

                if self.sessions:  # last, so an aborted run already has all other results
                    self._phase("session")
                    settings["sandbox"] = sandbox.description()
                    self._log("[b]Phase 3:[/] long sessions (deviation) - the model's code runs in a sandbox: "
                              + settings["sandbox"])
                    for p in self.sessions:
                        await self._session(client, db, run_id, p)
            status = "done"
        except asyncio.CancelledError:
            status = "aborted"
            self._log(f"[{self._colour(db, run_id, settings)}]Aborted.[/]")
            raise
        except SettingRejected as e:
            self.stop_batch = settings["error"] = str(e)
            self._log(f"[{self._colour(db, run_id, settings)}]Error:[/] {Text(str(e)).markup}")
        except Exception as e:
            settings["error"] = str(e)  # the reason stays with the run (it used to be only in the live log)
            self._log(f"[{self._colour(db, run_id, settings)}]Error:[/] {e}")
        finally:
            if live:
                live.cancel()
            if kv:
                kv.cancel()
                settings["kv_peak"] = self.kv_peak
            if self.clamped:
                settings["clamped"] = self.clamped
            await self.monitor.stop()
            if status == "done" and not storage.run_usable(db, run_id, settings) and (
                    db.execute("SELECT 1 FROM results WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() or settings.get("report")):
                status = "failed"  # it ran, but every request failed: nothing to use
                settings["error"] = settings.get("error") or "Every request failed - nothing usable came back (see the log of the test)."
                self._log(f"[red]Error:[/] {settings['error']}")
            usable = storage.run_usable(db, run_id, settings)
            gpu_info = ""
            if self.monitor.samples:
                gpu_info = ", ".join(f"{g.name} ({g.mem_total / 1024:.0f} GB{' shared with the CPU' if g.shared else ''})"
                                     for g in self.monitor.samples[-1].gpus)
            db.executemany(
                "INSERT INTO samples (run_id, t, phase, cpu_pct, mem_used_mb, mem_total_mb, gpus) VALUES (?,?,?,?,?,?,?)",
                [(run_id, s.t - t_run, self._phase_at(s.t), s.cpu_pct, s.mem_used_mb, s.mem_total_mb,
                  json.dumps([asdict(g) for g in s.gpus])) for s in self.monitor.samples],
            )
            db.execute("UPDATE runs SET finished = ?, status = ?, settings = ?, gpu_info = ? WHERE id = ?",
                       (datetime.now().isoformat(timespec="seconds"), status, json.dumps(settings), gpu_info, run_id))
            db.commit()
            db.close()
            self.emit("finished", run_id=run_id, status=status, usable=usable)
        return run_id


class BatchRunner:
    """Tests several models of one endpoint one after another; each model - and each combination of the multi-valued
    settings (max_tokens, temperature, thinking, num_ctx; see config.batch_plan) - is stored as its own run.

    With a single model and single values it behaves exactly like a BenchmarkRunner. If the endpoint refuses a setting
    (e.g. a thinking level), the batch stops after that run's message; the runs up to there stay in the database.
    """

    def __init__(self, ep: Endpoint, models: list[str], prompts: list[Prompt], settings: BenchSettings,
                 emit: EventCallback, capabilities: bool = False, per_model: dict[str, BenchSettings] | None = None):
        """per_model: settings of the models that differ from `settings` (Test tab: a model's own parameters)."""
        self.ep = ep
        self.models = models
        self.emit = emit
        self.offset = 0  # progress steps of the runs already done
        plans = {m: batch_plan(ep, (per_model or {}).get(m, settings)) for m in models}
        total = sum(len(p) for p in plans.values())
        self.runners = []
        for m in models:
            for step in plans[m]:
                note = self._note(step["note"], len(self.runners) + 1, total) if len(plans[m]) > 1 else ""
                self.runners.append(BenchmarkRunner(step["endpoint"], m, prompts, step["settings"], self._emit, capabilities, note=note))
        self.total_steps = sum(r.total_steps for r in self.runners)

    @staticmethod
    def _note(note: str, n: int, total: int) -> str:
        """'batch 2/4 · thinking high' of one model's plan -> numbered over the whole batch (all models)."""
        if total <= 1:
            return ""
        rest = note.split(" · ", 1)[1] if " · " in note else ""
        return f"batch {n}/{total}" + (f" · {rest}" if rest else "")

    def _emit(self, kind: str, **d) -> None:
        if kind == "progress":  # progress across the whole batch
            d = {"done": self.offset + d["done"], "total": self.total_steps}
        self.emit(kind, **d)

    async def run(self) -> list[int]:
        batch = len(self.runners) > 1
        run_ids = []
        for i, runner in enumerate(self.runners):
            if batch:
                self.emit("log", msg=f"[b]===== Run {i + 1}/{len(self.runners)}: {runner.model}"
                                     f"{' · ' + runner.note.split(' · ', 1)[1] if ' · ' in runner.note else ''} =====[/]")
            run_ids.append(await runner.run())  # a failing model is stored as failed; the batch goes on
            self.offset += runner.total_steps
            self.emit("progress", done=self.offset, total=self.total_steps)
            if runner.stop_batch:
                self.emit("log", msg=f"[red]Batch stopped:[/] {Text(runner.stop_batch).markup}\n"
                                     f"The {len(run_ids)} run(s) up to here are stored: {', '.join(f'#{r}' for r in run_ids)} "
                                     f"(the last one as failed); {len(self.runners) - i - 1} not started.")
                if self.ep.ollama_like:
                    try:
                        await unload_model(runner.ep, runner.model)
                    except httpx.HTTPError:
                        pass
                return run_ids
            last_of_model = i + 1 == len(self.runners) or self.runners[i + 1].model != runner.model
            if batch and last_of_model and self.ep.ollama_like:
                # free the GPU memory, so the next model does not share it with this one
                try:
                    await unload_model(runner.ep, runner.model)
                    self.emit("log", msg=f"Unloaded {runner.model}.")
                except httpx.HTTPError as e:
                    self.emit("log", msg=f"[yellow]Could not unload {runner.model}:[/] {type(e).__name__}")
        if batch:
            self.emit("log", msg=f"[b]Batch finished:[/] runs {', '.join(f'#{r}' for r in run_ids)}")
        return run_ids
