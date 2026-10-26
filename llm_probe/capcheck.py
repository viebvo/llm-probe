"""Capability check: what a model can do and which request settings the endpoint obeys.

A short series of small requests that probe what a model can do and which request settings the endpoint obeys.
Runs as a phase inside a benchmark (see bench.py); the findings are stored in the run's settings["report"].
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import httpx

from .check import reasoning_share, strip_thinking
from .client import (RequestResult, chat_stream, context_info, effort_rejected, listed_efforts, model_capabilities,
                     setting_rejected,
                     thinking_unsupported, unsupported_capability, unsupported_effort, uses_native_api)
from .config import BenchSettings, Endpoint
from .prompts import TOOLS, all_prompts, image_b64, image_mime
from . import storage

EventCallback = Callable[..., None]

YES, PARTIAL, NO, INFO, ERROR = "yes", "partial", "no", "info", "error"
ANSWER_CHARS = 2500  # how much of a probe's answer is kept for "show answers" (reasoning models write far more)


@dataclass
class Finding:
    group: str  # "capability" | "setting"
    name: str
    status: str  # yes | partial | no | info | error
    detail: str
    answers: list[dict] = field(default_factory=list)  # what the probe asked and what the model answered (for "show answers")


SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"},
                                           "population_millions": {"type": "number"}},
          "required": ["city", "country", "population_millions"], "additionalProperties": False}


def _answer(r: RequestResult) -> str:
    return strip_thinking(r.text)


def _short(text: str, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return repr(text if len(text) <= n else text[:n - 3] + "...")


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


MAX_EXTRA_LEVELS = 8  # further levels a server lists (see probe_reasoning)
REASONING_LEVELS = ["off", "low", "medium", "high", "xhigh"]  # what the Test tab can ask for, in the order they are tried


async def probe_reasoning(ep: Endpoint, model: str, timeout: float = 90.0) -> dict:
    """The reasoning query of the Test tab: does the model reason, and which thinking settings does the endpoint accept for it?

    First the server's own declaration (Ollama /api/show lists "thinking" among the capabilities - no request needed
    when it is not there). Otherwise a few tiny requests, which load the model on the endpoint: one without a setting
    (does it reason by itself?), then "off" and the effort levels (a level is accepted when the request goes through;
    when a server lists the levels it supports in its error, that list is taken). Returns {"reasons": bool,
    "efforts": [accepted settings], "off_ignored": the model reasons although "off" is accepted ("always on"), "source",
    "note", "checked"}; raises httpx.HTTPError / RuntimeError when the
    endpoint cannot be asked at all.
    """
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    caps, where = await model_capabilities(ep, model)
    if caps is not None and "thinking" not in caps:
        return {"reasons": False, "efforts": [], "source": "declared", "checked": now,
                "note": f"{where} declares no thinking capability for this model - no request was sent"}
    prompt = "What is 17 * 23? Answer with the number only."
    reasoned, accepted, listed, off_ignored = False, [], None, False
    async with httpx.AsyncClient() as client:
        async def ask(mode: str) -> RequestResult:
            return await chat_stream(client, ep, model, prompt, 200, 0.0, timeout, thinking=mode)

        first = await ask("")
        if not first.ok:
            raise RuntimeError(first.error[:200] or "the request failed")
        reasoned = reasoning_share(first.text) > 0
        for mode in REASONING_LEVELS:
            r = await ask(mode)
            if r.ok:
                accepted.append(mode)
                thought = reasoning_share(r.text) > 0
                reasoned = reasoned or thought
                off_ignored = off_ignored or (mode == "off" and thought)  # accepted, but the model reasons anyway
                continue
            if thinking_unsupported(r.error):  # Ollama: this model has no thinking at all
                accepted = []
                break
            if listed is None and (names := unsupported_effort(r.error)):
                listed = names  # the server names the levels it knows: take them (lowest first)
                accepted = [m for m in accepted if m == "off"] + [n for n in names if n in REASONING_LEVELS]
                break
            if not effort_rejected(r.error) and not setting_rejected(r.error):
                raise RuntimeError(r.error[:200] or "the request failed")
        # other levels than the usual ones: a deliberately invalid value makes the server list the valid ones
        extra: list[str] = []
        if reasoned:
            bad = await ask("not-a-level")
            if not bad.ok:
                for name in listed_efforts(bad.error):
                    if name in accepted or name in REASONING_LEVELS or len(extra) >= MAX_EXTRA_LEVELS:
                        continue
                    r = await ask(name)
                    if r.ok:
                        extra.append(name)
                accepted += extra
    if not reasoned:
        return {"reasons": False, "efforts": [], "source": "probe", "checked": now,
                "note": "no reasoning seen in any answer: thinking settings do not apply to this model"}
    return {"reasons": True, "efforts": accepted, "off_ignored": off_ignored, "source": "probe", "checked": now,
            "note": "accepted: " + (", ".join(accepted) or "none") + (" (as listed by the server)" if listed else "")
            + (f"; found by asking the server for the valid values: {', '.join(extra)}" if extra else "")
            + ("; 'off' is accepted but the model reasons anyway" if off_ignored else "")}


class CapabilityProbe:
    """Probes one model's capabilities, returning a list of Findings. Runs on a client provided by the caller
    (bench.py), so it shares the benchmark's connection and run row."""

    def __init__(self, ep: Endpoint, model: str, settings: BenchSettings, client: httpx.AsyncClient,
                 emit: EventCallback | None = None, on_step: Callable[[], None] | None = None):
        self.ep = ep
        self.model = model
        self.s = settings
        self.emit = emit or (lambda *a, **k: None)
        self._on_step = on_step or (lambda: None)
        self.client = client
        self.checks: list[tuple[str, str, Callable[[], Awaitable[tuple[str, str]]]]] = [
            ("capability", "Vision (images)", self._vision),
            ("capability", "Tool calling", self._tools),
            ("capability", "Reasons by default", self._thinks),
            ("setting", "Thinking off", self._thinking_off),
            ("setting", "Thinking effort (low / high)", self._thinking_levels),
            ("setting", "JSON mode", self._json_mode),
            ("setting", "JSON schema (structured output)", self._json_schema),
            ("setting", "System prompt", self._system),
            ("setting", "max_tokens limit", self._max_tokens),
            ("setting", "Stop sequences", self._stop),
            ("setting", "Seed (same seed, same output)", self._seed),
            ("setting", "Temperature 0 (deterministic)", self._greedy),
        ]
        self.usage: list[bool] = []  # per request: token counts reported by the server
        self.asked: list[dict] = []  # the requests of the probe that is running (stored with its finding)
        self.reasons = False  # the model reasons by default (set by _thinks)

    def _log(self, msg: str) -> None:
        self.emit("log", msg=msg)

    def _step(self) -> None:
        self._on_step()

    async def _ask(self, prompt: str, max_tokens: int = 1500, thinking: str | None = None, **kw) -> RequestResult:
        temperature, seed = kw.pop("temperature", 0.0), kw.pop("seed", 1234)
        thinking = self.s.thinking if thinking is None else thinking
        r = await chat_stream(self.client, self.ep, self.model, prompt, max_tokens, temperature, self.s.request_timeout,
                              seed=seed, thinking=thinking, **kw)
        if r.ok:
            self.usage.append(r.tokens_from_usage)
        sent = [f"max_tokens {max_tokens}", f"temperature {temperature}", f"seed {seed}"] + ([f"thinking {thinking}"] if thinking else []) + (["tools"] if kw.get("tools") else []) + \
            (["image"] if kw.get("images") else []) + ([f"extra {json.dumps(kw['extra'])[:90]}"] if kw.get("extra") else [])
        self.asked.append({"prompt": prompt[:400], "sent": ", ".join(sent),
                           "answer": (r.text or "")[:ANSWER_CHARS] if r.ok else f"[error] {r.error[:300]}",
                           "tool_calls": r.tool_calls if r.ok and r.tool_calls else None})
        return r

    @staticmethod
    def _refused(r: RequestResult) -> str:
        return f"refused: {r.error[:140]}"

    def _no_reasoning(self, r: RequestResult) -> tuple[str, str]:
        """A thinking setting was refused. 'does not support thinking' means the model cannot reason at all;
        a rejected effort value (the model reasons at other settings) is reported yellow as 'not supported',
        not as a hard error - until the reasoning effort becomes a per-model setting (see per-model-temperature)."""
        if thinking_unsupported(r.error):
            return (INFO, "not applicable - the model has no reasoning") if not self.reasons else (NO, self._refused(r))
        if effort_rejected(r.error):
            return PARTIAL, f"effort not supported by this model/server: {r.error[:120]}"
        return ERROR, r.error[:160]

    # ------------------------------------------------------------ capabilities

    async def _vision(self) -> tuple[str, str]:
        r = await self._ask("Which city name is printed on the door of the white truck? Answer with the city name only.",
                            images=[(image_mime("street.jpg"), image_b64("street.jpg"))])
        if not r.ok:
            return (NO if unsupported_capability(r.error) == "vision" else ERROR), self._refused(r)
        answer = _answer(r)
        if "amsterdam" in answer.casefold():
            return YES, f"read the image correctly ({_short(answer, 30)})"
        return PARTIAL, f"accepts images, but answered {_short(answer)} (expected Amsterdam)"

    async def _tools(self) -> tuple[str, str]:
        p = next(p for p in all_prompts() if p.id == "tool_weather_en")
        r = await self._ask(p.text, tools=TOOLS)
        if not r.ok:
            return (NO if unsupported_capability(r.error) == "tools" else ERROR), self._refused(r)
        if not r.tool_calls:
            return NO, f"tools accepted, but answered without calling one: {_short(_answer(r))}"
        c = p.evaluate(r.text, r.tool_calls)
        calls = ", ".join(storage.call_label(x) for x in r.tool_calls)
        return (YES if c and c.exact else PARTIAL), calls

    async def _thinks(self) -> tuple[str, str]:
        r = await self._ask("What is 17 * 23? Answer with the number only.", thinking="")
        if not r.ok:
            return ERROR, r.error[:160]
        share = reasoning_share(r.text)
        self.reasons = share > 0
        if share:
            return YES, f"reasoned without being asked ({share * 100:.0f} % of {r.completion_tokens} tokens)"
        return NO, f"answered directly ({r.completion_tokens} tokens)"

    # ------------------------------------------------------------ settings

    async def _thinking_off(self) -> tuple[str, str]:
        r = await self._ask("What is 17 * 23? Answer with the number only.", thinking="off")
        if not r.ok:
            return self._no_reasoning(r)
        share = reasoning_share(r.text)
        if share:
            return NO, f"ignored - still reasoned ({share * 100:.0f} % of the output)"
        return YES, f"no reasoning ({r.completion_tokens} tokens)"

    async def _thinking_levels(self) -> tuple[str, str]:
        prompt = "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. What does the ball cost?"
        levels = ["low", "high"]  # compare a low vs a high effort; renamed below if the server lists its own
        reasoning = []
        for i, level in enumerate(levels):
            r = await self._ask(prompt, max_tokens=4000, thinking=level)
            if not r.ok:
                supported = unsupported_effort(r.error)
                if not supported:
                    return self._no_reasoning(r)
                # the server names its own effort levels (e.g. xhigh/medium/low): lowest for the low slot, highest
                # for the high slot, so the test still contrasts least vs most effort
                level = supported[0] if i == 0 else supported[-1]
                levels[i] = level
                r = await self._ask(prompt, max_tokens=4000, thinking=level)
                if not r.ok:
                    return self._no_reasoning(r)
            reasoning.append((r.completion_tokens or 0) * reasoning_share(r.text))
        lo_name, hi_name = levels
        lo_tok, hi_tok = reasoning
        detail = f"reasoning ~{lo_tok:.0f} tokens ({lo_name}) vs ~{hi_tok:.0f} ({hi_name})"
        if not hi_tok:
            if not self.reasons:
                return INFO, "not applicable - the model does not reason"
            return NO, "accepted, but no reasoning at all - " + detail
        if hi_tok > lo_tok * 1.3:
            return YES, detail
        return PARTIAL, "accepted, but no clear effect - " + detail

    async def _json_mode(self) -> tuple[str, str]:
        extra = {"format": "json"} if uses_native_api(self.ep) else {"response_format": {"type": "json_object"}}
        r = await self._ask("Describe a cat in a few words.", extra=extra)  # no hint at JSON: the switch must do it
        if not r.ok:
            return NO, self._refused(r)
        answer = _answer(r)
        data = _json(answer)
        if isinstance(data, dict):
            return (YES, f"valid JSON object: {_short(answer)}") if data else \
                (PARTIAL, "valid, but an empty object {}")
        return NO, f"ignored - not JSON: {_short(answer)}"

    async def _json_schema(self) -> tuple[str, str]:
        if uses_native_api(self.ep):
            extra = {"format": SCHEMA}
        else:
            extra = {"response_format": {"type": "json_schema",
                                         "json_schema": {"name": "city", "schema": SCHEMA, "strict": True}}}
        r = await self._ask("Tell me about the largest city of Austria.", extra=extra)
        if not r.ok:
            return NO, self._refused(r)
        answer = _answer(r)
        data = _json(answer)
        if not isinstance(data, dict):
            return NO, f"ignored - not JSON: {_short(answer)}"
        ok = set(data) == set(SCHEMA["properties"]) and isinstance(data.get("population_millions"), (int, float))
        return (YES, f"matches the schema: {_short(answer)}") if ok else \
            (PARTIAL, f"JSON, but not the schema: {_short(answer)}")

    async def _system(self) -> tuple[str, str]:
        r = await self._ask("What is the capital of France?",
                            system="You are a pirate parrot. Whatever you are asked, reply with the single word "
                                   "ARRR and nothing else.")
        if not r.ok:
            return ERROR, r.error[:160]
        answer = _answer(r)
        return (YES if "arrr" in answer.casefold() else NO), f"answered {_short(answer)}"

    async def _max_tokens(self) -> tuple[str, str]:
        r = await self._ask("Write a long story about a dragon who learns to cook.", max_tokens=20)
        if not r.ok:
            return ERROR, r.error[:160]
        n = r.completion_tokens or 0
        detail = f"{n} tokens{'' if r.tokens_from_usage else ' (estimated)'}, finish reason '{r.finish_reason or '-'}'"
        return (YES if n <= 22 else NO), detail

    async def _stop(self) -> tuple[str, str]:
        extra = {"options": {"stop": ["7"]}} if uses_native_api(self.ep) else {"stop": ["7"]}
        r = await self._ask("Count from 1 to 12, separated by spaces. Output only the numbers.", extra=extra)
        if not r.ok:
            return NO, self._refused(r)
        # an obeyed stop sequence ends the output (reasoning included) before its first "7"
        if "7" in r.text:
            return NO, f"ignored - the output contains '7': {_short(_answer(r) or r.text)}"
        return YES, f"stopped before '7': {_short(_answer(r) or r.text)}"

    async def _seed(self) -> tuple[str, str]:
        prompt = "Invent a name for a fantasy city. Reply with the name only."
        runs = [await self._ask(prompt, max_tokens=600, temperature=1.0, seed=seed) for seed in (42, 42, 7, 99)]
        if not all(r.ok for r in runs):
            return ERROR, next(r.error for r in runs if not r.ok)[:160]
        a, b, c, d = (_answer(r) or r.text for r in runs)
        if a != b:
            return NO, f"same seed, different outputs at temperature 1: {_short(a, 25)} / {_short(b, 25)}"
        if a == c == d:
            return PARTIAL, f"reproducible, but other seeds give the same output too: {_short(a, 30)}"
        return YES, f"seed 42 twice: {_short(a, 25)}; seeds 7 / 99: {_short(c, 20)} / {_short(d, 20)}"

    async def _greedy(self) -> tuple[str, str]:
        prompt = "Write one sentence about the sea."
        a, b = [await self._ask(prompt, max_tokens=600, seed=seed) for seed in (1, 2)]
        if not (a.ok and b.ok):
            return ERROR, (a.error or b.error)[:160]
        same = _answer(a) == _answer(b)
        return (YES if same else NO), ("identical with different seeds" if same else
                                       f"outputs differ: {_short(_answer(a), 30)} / {_short(_answer(b), 30)}")

    # ------------------------------------------------------------ flow

    async def report(self) -> list[dict]:
        """Run all probes for this model and return the findings as dicts (for settings["report"]). The caller has
        already warmed the model up; this adds no warmup."""
        report: list[Finding] = []
        declared, source = await model_capabilities(self.ep, self.model)
        report.append(Finding("capability", "Declared by the server", INFO,
                              f"{', '.join(declared)} ({source})" if declared is not None
                              else "the server does not say (vLLM / OpenAI-compatible) - see the tests below"))
        ctx = await context_info(self.ep, self.model)
        report.append(Finding("capability", "Context length", INFO, storage.context_label({"context": ctx})))
        self._log(f"  {report[-1].name}: {report[-1].detail}")
        self._step()
        for group, name, fn in self.checks:
            self.asked = []
            try:
                st, detail = await fn()
            except (httpx.HTTPError, ValueError, KeyError) as e:
                st, detail = ERROR, f"{type(e).__name__}: {e}"
            report.append(Finding(group, name, st, detail, self.asked))
            style = {YES: "green", PARTIAL: "yellow", NO: "red", ERROR: "red"}.get(st, "dim")
            self._log(f"  {name}: [{style}]{st}[/] - {detail}")
            self._step()
        report.append(Finding("setting", "Token usage reported", YES if all(self.usage) else
                              PARTIAL if any(self.usage) else NO,
                              "the server counts the tokens" if all(self.usage) else
                              "estimated from stream chunks for some responses"))
        return [asdict(f) for f in report]


# the capability phase takes this many progress steps (one per check plus the declared/context step)
CAP_STEPS = 13
