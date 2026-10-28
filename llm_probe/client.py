"""OpenAI-compatible streaming client that measures latencies on the client side.

Both Ollama and vLLM are queried via /v1/chat/completions so that both
instances are measured in exactly the same way. Exception: an Ollama endpoint
with a fixed context length (num_ctx) uses Ollama's native /api/chat, because
the OpenAI-compatible API offers no way to set it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .check import answer_offset
from .config import Endpoint


DeltaCallback = Callable[[str, str], None]  # (content, reasoning) of one stream chunk


@dataclass
class RequestResult:
    ok: bool
    ttft: float | None = None  # seconds until the first token (reasoning counts)
    ttfa: float | None = None  # seconds until the first token of the answer itself (after the reasoning)
    total: float | None = None  # seconds until the last token
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_from_usage: bool = False  # False = tokens estimated from stream chunks
    text: str = ""
    error: str = ""
    started_at: float = 0.0  # time.monotonic() when sent
    finish_reason: str = ""
    limit: str = ""  # the response was cut off: "tokens" = at max_tokens, "time" = at the time limit
    chunks: int = 0  # stream chunks with output
    tool_calls: list[dict] = field(default_factory=list)  # [{"name", "arguments"}]; arguments a dict if valid JSON
    marks: list[float] = field(default_factory=list, repr=False)  # monotonic arrival time per chunk (internal)
    t_tool: float | None = field(default=None, repr=False)  # monotonic time of the first tool call chunk (internal)

    @property
    def decode_tps(self) -> float | None:
        """Generation speed excluding prompt processing (tokens after the first / time after it).
        None for output that arrived in one chunk (e.g. a tool call only): there is no generation time."""
        if not self.ok or not self.completion_tokens or self.completion_tokens < 2 or self.ttft is None \
                or self.chunks < 2:
            return None
        dt = self.total - self.ttft
        return (self.completion_tokens - 1) / dt if dt > 0 else None

    @property
    def e2e_tps(self) -> float | None:
        if not self.ok or not self.completion_tokens or not self.total:
            return None
        return self.completion_tokens / self.total


def _headers(ep: Endpoint) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if ep.api_key:
        h["Authorization"] = f"Bearer {ep.api_key}"
    return h


def api_error_hint(ex: Exception) -> str:
    """Likely cause of a failed API call, for error messages."""
    if isinstance(ex, httpx.HTTPStatusError) and ex.response.status_code in (401, 403):
        return "Authentication failed - API key missing or wrong."
    return "Is the instance running?"


def thinking_params(ep: Endpoint, mode: str, native: bool = False) -> dict:
    """Request fields that switch reasoning off or set its effort (mode: off | low | medium | high, "" = nothing).

    Hybrid models (e.g. Qwen3) honour "off"; pure reasoning models (DeepSeek-R1, gpt-oss, Qwen3 "thinking"
    variants) think anyway - the results show whether a model actually did.
    """
    if not mode:
        return {}
    if native:  # Ollama /api/chat (also via Open WebUI's passthrough)
        return {"think": False if mode == "off" else mode}
    if ep.kind == "openwebui":  # Open WebUI drops unknown top-level fields, but passes "params" on to Ollama
        return {"params": {"think": False if mode == "off" else mode}}
    if ep.kind == "vllm" and mode == "off":  # Qwen3-style chat templates; vLLM rejects reasoning_effort "none"
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"reasoning_effort": "none" if mode == "off" else mode}  # Ollama maps it to "think"


def thinking_unsupported(error: str) -> bool:
    """Ollama refuses a thinking level for models without reasoning."""
    return "does not support thinking" in error


# reasoning effort levels, lowest first; a server may offer only a subset and name them itself (see below)
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh"]


def unsupported_effort(error: str) -> list[str]:
    """When a server rejects a reasoning_effort value because that effort is unknown, the effort levels it does
    list as supported (lowest first, known levels only); [] if the error is about something else.

    Servers phrase this differently, e.g.
      vLLM:   "Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low."
      Ollama: "`reasoning_effort` must be one of `low`, `medium`, or `high`"
    """
    low = error.lower()
    if "reasoning effort" not in low and "reasoning_effort" not in low:
        return []
    m = re.search(r"supported (?:types|values|efforts)\s+are\s+(.+)", low) or re.search(r"must be one of\s+(.+)", low)
    if not m:
        return []
    names = {n for n in re.findall(r"[a-z]+", m.group(1)) if n in EFFORT_ORDER}
    return [e for e in EFFORT_ORDER if e in names]


_NOT_A_LEVEL = {"or", "and", "must", "be", "one", "of", "default", "the", "types", "values", "supported", "none", "are", "is", "valid"}


def listed_efforts(error: str) -> list[str]:
    """The thinking levels a server names when it rejects a value, in the order it lists them and whatever they are
    called (unsupported_effort only knows the usual ones), e.g. Ollama:
      invalid reasoning value: "banana" (must be "minimal", "low", "medium", "high", "xhigh", "ultra", "max")
    [] if the error does not list any."""
    error = error.replace("\\", "")  # the message arrives inside a JSON body: \"low\"
    m = re.search(r"(?:must be(?: one of)?|supported (?:types|values|efforts)\s+are|valid (?:values|options)\s*(?:are|:))\s*(.+)",
                  error, re.I | re.S)
    if not m:
        return []
    text = re.split(r"[\"'`]\)", m.group(1))[0] + '"'  # (the rest of a JSON body is not a level)
    names = re.findall(r"[\"'`]([a-z0-9_-]{1,24})[\"'`]", text) or re.findall(r"[a-z0-9_-]{1,24}", text.lower())
    return [n for n in dict.fromkeys(names) if n not in _NOT_A_LEVEL]


def effort_rejected(error: str) -> bool:
    """The reasoning_effort / thinking value was rejected by the server or the model's chat template (so the
    model may still reason at other settings) - as opposed to the model not reasoning at all (thinking_unsupported).
    Covers the "must be one of ..." reply and a Jinja chat template that raises on an unknown effort (HTTP 500)."""
    low = error.lower()
    return any(s in low for s in ("reasoning_effort", "reasoning effort", "enable_thinking", "raise_exception",
                                  "must be one of"))


_SETTING_WORDS = ("max_tokens", "max_completion_tokens", "temperature", "num_ctx", "reasoning", "thinking", "think ")


def setting_rejected(error: str) -> bool:
    """The error says that a setting WE sent (thinking / reasoning effort, max_tokens, temperature, num_ctx) was refused -
    a batch run stops there instead of testing the next combination. 'The model has no reasoning' is not one (the
    run goes on without the setting, see thinking_unsupported)."""
    low = (error or "").lower()
    if thinking_unsupported(error or ""):
        return False
    return effort_rejected(error or "") or (("400" in low or "422" in low or "invalid" in low or "unsupported" in low)
                                            and any(w in low for w in _SETTING_WORDS))


# how servers refuse a request the model cannot handle (Ollama, vLLM)
_UNSUPPORTED = {
    "vision": ("does not support multimodal", "does not support image", "not a multimodal model",
               "image input is not supported", "does not support vision"),
    "tools": ("does not support tools", "tool choice requires", "tool_choice requires", "tools are not supported"),
}


def unsupported_capability(error: str) -> str:
    """'vision' / 'tools' if the error says the model (or server) cannot do that, else ""."""
    low = error.lower()
    return next((cap for cap, needles in _UNSUPPORTED.items() if any(n in low for n in needles)), "")


async def model_capabilities(ep: Endpoint, model: str, timeout: float = 10.0) -> tuple[list[str] | None, str]:
    """Capabilities the server declares for the model, e.g. ['completion', 'vision', 'tools', 'thinking'], and
    where they come from; None = the server does not say (vLLM, OpenAI-compatible)."""
    if not ep.ollama_like:
        return None, ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if ep.kind == "openwebui":  # Open WebUI passes Ollama's model entry on (its own "meta" flags are UI switches)
                caps = (await openwebui_ollama_model(client, ep, model)).get("capabilities")
                return (caps, "Open WebUI (from Ollama)") if caps is not None else (None, "")
            show = await _get_json(client, ep, f"{ep.native_base}/api/show", {"model": model})
            caps = show.get("capabilities")
            return (caps, "ollama /api/show") if caps is not None else (None, "")
        except (httpx.HTTPError, ValueError):
            return None, ""


# what a model name says about its quantization / size (a hint, not a finding: used only if the server does not say)
_QUANT_NAME = re.compile(r"(?i)(?<![a-z0-9])(nvfp4|mxfp4|fp8|fp4|int4|int8|awq|gptq|bnb|iq\d_[a-z]+|q\d(?:_k(?:_[sml])?|_[01])?|bf16|f16)(?![a-z0-9])")
_SIZE_NAME = re.compile(r"(?i)(?<![a-z0-9.])(\d{1,4}(?:\.\d{1,3})?)\s?b(?![a-z0-9])")


_UNITS = {"": 1e-9, "K": 1e-6, "M": 1e-3, "B": 1.0, "T": 1e3}  # parameter counts in billions


def billions(size: str) -> str:
    """A parameter size always in billions, e.g. '134.52M' -> '0.13B', '7.62B' -> '7.62B', '1.2T' -> '1200B'; '' stays ''.
    One unit everywhere makes sizes comparable at a glance."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMBT]?)\s*", str(size or ""), re.I)
    if not m:
        return str(size or "")
    try:
        b = float(m.group(1)) * _UNITS[m.group(2).upper()]
    except ValueError:
        return str(size)
    return f"{b:.{3 if b < 0.1 else 2}f}".rstrip("0").rstrip(".") + "B"


def guess_details(model: str) -> dict:
    """Quantization and parameter size as far as the model name tells (source 'guessed')."""
    model = model[:200]  # (a model name is short; the cut keeps the searches below cheap whatever a server sends)
    q, n = _QUANT_NAME.search(model), _SIZE_NAME.search(model)
    return {"quant": q.group(1).upper() if q else "", "size": f"{n.group(1)}B" if n else "",
            "quant_source": "guessed" if q else "", "size_source": "guessed" if n else ""}


async def model_contexts(ep: Endpoint, timeout: float = 5.0) -> dict[str, int]:
    """The context length each model of a vLLM-like server reports (`max_model_len` in /v1/models); {} if the server says none.
    It can be read, but not set from here (it is chosen when the server starts)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = (await _get_json(client, ep, f"{ep.api_base}/models")).get("data", [])
    except (httpx.HTTPError, ValueError):
        return {}
    return {m["id"]: int(m["max_model_len"]) for m in data if m.get("id") and m.get("max_model_len")}


async def model_details(ep: Endpoint, model: str, timeout: float = 5.0) -> dict:
    """{quant, size, quant_source, size_source, capabilities}: Ollama (also behind Open WebUI) declares quantization level,
    parameter size and capabilities in /api/show; for other servers (or what the server leaves out) the model name is read. "" = unknown."""
    out = {"quant": "", "size": "", "quant_source": "", "size_source": "", "capabilities": None}
    if ep.ollama_like:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                if ep.kind == "openwebui":
                    show = await openwebui_ollama_model(client, ep, model)
                else:
                    show = await _get_json(client, ep, f"{ep.native_base}/api/show", {"model": model})
                details = show.get("details") or {}
                out["capabilities"] = show.get("capabilities")  # e.g. ["completion", "vision", "tools"]; None = the server does not say
            except (httpx.HTTPError, ValueError):
                details = {}
        for key, name in (("quantization_level", "quant"), ("parameter_size", "size")):
            if details.get(key):
                out[name], out[name + "_source"] = (billions(details[key]) if name == "size" else str(details[key])), "declared"
    guess = guess_details(model)
    for name in ("quant", "size"):
        if not out[name] and guess[name]:
            out[name], out[name + "_source"] = guess[name], "guessed"
    return out


def _merge(payload: dict, extra: dict) -> dict:
    """Add request fields; an "options" dict (native Ollama API) is merged into the existing one."""
    for k, v in extra.items():
        payload[k] = payload[k] | v if k == "options" and k in payload else v
    return payload


def _messages(prompt: str, images: list[tuple[str, str]], turns: list[dict], native: bool,
              system: str = "") -> list[dict]:
    """Chat messages: the user prompt with its images ((mime, base64) each), then further turns
    (assistant tool calls, tool results) in the format of the API."""
    if native:
        user = {"role": "user", "content": prompt} | ({"images": [b64 for _, b64 in images]} if images else {})
    elif images:
        user = {"role": "user", "content": [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}} for mime, b64 in images]}
    else:
        user = {"role": "user", "content": prompt}
    out, ids = ([{"role": "system", "content": system}] if system else []) + [user], []
    for t in turns:
        if t["role"] == "assistant" and t.get("tool_calls"):
            if native:
                calls = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in t["tool_calls"]]
            else:
                calls = [{"id": f"call_{len(ids) + i}", "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                         for i, c in enumerate(t["tool_calls"])]
                ids += [c["id"] for c in calls]
            out.append({"role": "assistant", "content": t.get("content", ""), "tool_calls": calls})
        elif t["role"] == "tool":  # answers the oldest open tool call
            out.append({"role": "tool", "content": t["content"]}
                       | ({"tool_name": t.get("name", "")} if native else {"tool_call_id": ids.pop(0) if ids else ""}))
        else:
            out.append({"role": t["role"], "content": t.get("content", "")})
    return out


def _parse_args(args) -> dict | str:
    if isinstance(args, str):
        try:
            return json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return args  # kept as text: the check counts it as wrong
    return args


async def list_models(ep: Endpoint, timeout: float = 10.0) -> list[str]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{ep.api_base}/models", headers=_headers(ep))
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))


def _stalled(res: RequestResult, e: httpx.TimeoutException, timeout: float) -> bool:
    """The server sent nothing for `timeout` seconds. True = keep what arrived before as a response cut off at the
    time limit; False = nothing arrived, the request failed (res.error is set)."""
    res.limit = "time"
    if res.ttft is None:
        res.error = f"{type(e).__name__}: no output within the time limit of {timeout:g} s"
        return False
    return True


def _finish(res: RequestResult, parts: list[str], reasoning: list[str], chunks: int, t0: float,
            t_last: float) -> RequestResult:
    """parts: content of every chunk (reasoning: its reasoning), res.marks: arrival time of every chunk."""
    res.total = t_last - t0
    offset = answer_offset("".join(parts))  # skips reasoning the server left inline in the content
    if offset is not None:
        size = 0
        for t, part in zip(res.marks, parts):
            size += len(part)
            if size > offset:
                res.ttfa = t - t0
                break
    if res.t_tool is not None and (res.ttfa is None or res.t_tool - t0 < res.ttfa):
        res.ttfa = res.t_tool - t0  # a tool call is the answer, too
    if res.finish_reason == "length":
        res.limit = res.limit or "tokens"
    res.marks = []
    res.chunks = chunks
    if not res.tokens_from_usage:
        res.completion_tokens = chunks
    think_text = "".join(reasoning).strip()
    res.text = (f"<think>\n{think_text}\n</think>\n\n" if think_text else "") + "".join(parts)
    res.ok = res.ttft is not None
    if not res.ok:
        res.error = res.error or "No tokens received"
    return res


def uses_native_api(ep: Endpoint) -> bool:
    """Requests go to Ollama's native /api/chat instead of the OpenAI-compatible API (see module docstring)."""
    return bool(ep.ollama_num_ctx)


async def chat_stream(
    client: httpx.AsyncClient,
    ep: Endpoint,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
    seed: int = 1234,
    on_delta: DeltaCallback | None = None,
    thinking: str = "",
    images: list[tuple[str, str]] | None = None,
    tools: list[dict] | None = None,
    turns: list[dict] | None = None,
    system: str = "",
    extra: dict | None = None,
) -> RequestResult:
    """Stream one chat completion. on_delta is called after each chunk has been timestamped (live display).

    images: (mime type, base64) sent with the prompt; tools: OpenAI function definitions; turns: further turns
    after the prompt (see prompts.Prompt.turns); system: system prompt; extra: further request fields in the
    format of the API used (see uses_native_api).
    """
    more = (images or [], tools or [], turns or [], system, extra or {})
    if uses_native_api(ep):
        return await _ollama_stream(client, ep, model, prompt, max_tokens, temperature, timeout, seed, on_delta,
                                    thinking, *more)
    payload = {
        "model": model,
        "messages": _messages(prompt, more[0], more[2], native=False, system=system),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
    } | thinking_params(ep, thinking) | ({"tools": tools} if tools else {})
    _merge(payload, more[4])
    res = RequestResult(ok=False)
    calls: dict[int, dict] = {}  # streamed tool calls by index: name and argument text arrive in pieces
    parts: list[str] = []
    reasoning: list[str] = []
    chunks = 0
    t0 = time.monotonic()
    res.started_at = t0
    t_last = t0
    try:
        async with client.stream(
            "POST", f"{ep.api_base}/chat/completions", json=payload, headers=_headers(ep), timeout=timeout
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")
                res.error = f"HTTP {r.status_code}: {body[:300]}"
                return res
            async for line in r.aiter_lines():
                if time.monotonic() - t0 > timeout:  # the time limit counts the whole request, not single reads
                    res.limit = "time"
                    break
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if usage := obj.get("usage"):
                    res.prompt_tokens = usage.get("prompt_tokens")
                    res.completion_tokens = usage.get("completion_tokens")
                    res.tokens_from_usage = True
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc.get("index", len(calls)), {"name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        call["name"] += fn.get("name") or ""
                        args = fn.get("arguments")
                        call["arguments"] += args if isinstance(args, str) else json.dumps(args) if args else ""
                        if res.t_tool is None:
                            res.t_tool = time.monotonic()
                            res.ttft = res.ttft if res.ttft is not None else res.t_tool - t0
                    if delta.get("tool_calls") and not (content or think):
                        t_last = time.monotonic()
                        chunks += 1
                    if content or think:
                        now = time.monotonic()
                        if res.ttft is None:
                            res.ttft = now - t0
                        t_last = now
                        chunks += 1
                        res.marks.append(now)
                        parts.append(content)
                        reasoning.append(think)
                        if on_delta:
                            on_delta(content, think)
                    if choice.get("finish_reason"):
                        res.finish_reason = choice["finish_reason"]
    except httpx.TimeoutException as e:
        if not _stalled(res, e, timeout):
            return res
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    res.tool_calls = [{"name": c["name"], "arguments": _parse_args(c["arguments"])} for _, c in sorted(calls.items())]
    return _finish(res, parts, reasoning, chunks, t0, t_last)


async def _ollama_stream(
    client: httpx.AsyncClient,
    ep: Endpoint,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
    seed: int,
    on_delta: DeltaCallback | None,
    thinking: str,
    images: list[tuple[str, str]],
    tools: list[dict],
    turns: list[dict],
    system: str,
    extra: dict,
) -> RequestResult:
    """Native Ollama /api/chat (NDJSON stream) with options.num_ctx; same measurement as chat_stream."""
    payload = {
        "model": model,
        "messages": _messages(prompt, images, turns, native=True, system=system),
        "stream": True,
        "options": {"num_ctx": ep.ollama_num_ctx, "num_predict": max_tokens, "temperature": temperature, "seed": seed},
    } | thinking_params(ep, thinking, native=True) | ({"tools": tools} if tools else {})
    _merge(payload, extra)
    res = RequestResult(ok=False)
    parts: list[str] = []
    reasoning: list[str] = []
    chunks = 0
    t0 = time.monotonic()
    res.started_at = t0
    t_last = t0
    try:
        async with client.stream(
            "POST", f"{ep.native_base}/api/chat", json=payload, headers=_headers(ep), timeout=timeout
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")
                res.error = f"HTTP {r.status_code}: {body[:300]}"
                return res
            async for line in r.aiter_lines():
                if time.monotonic() - t0 > timeout:  # the time limit counts the whole request, not single reads
                    res.limit = "time"
                    break
                if not line.strip():
                    continue
                obj = json.loads(line)
                if err := obj.get("error"):
                    res.error = f"Ollama: {err}"
                    break
                msg = obj.get("message") or {}
                content = msg.get("content") or ""
                think = msg.get("thinking") or ""
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    res.tool_calls.append({"name": fn.get("name", ""), "arguments": _parse_args(fn.get("arguments"))})
                    if res.t_tool is None:
                        res.t_tool = time.monotonic()
                        res.ttft = res.ttft if res.ttft is not None else res.t_tool - t0
                        t_last = res.t_tool
                if content or think:
                    now = time.monotonic()
                    if res.ttft is None:
                        res.ttft = now - t0
                    t_last = now
                    chunks += 1
                    res.marks.append(now)
                    parts.append(content)
                    reasoning.append(think)
                    if on_delta:
                        on_delta(content, think)
                if obj.get("done"):
                    res.finish_reason = obj.get("done_reason") or ""
                    if obj.get("eval_count") is not None:
                        res.prompt_tokens = obj.get("prompt_eval_count")
                        res.completion_tokens = obj["eval_count"]
                        res.tokens_from_usage = True
                    break
    except httpx.TimeoutException as e:
        if not _stalled(res, e, timeout):
            return res
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    if res.error:
        return res
    return _finish(res, parts, reasoning, chunks, t0, t_last)


async def _get_json(client: httpx.AsyncClient, ep: Endpoint, url: str, body: dict | None = None):
    r = await (client.post(url, json=body, headers=_headers(ep)) if body is not None
               else client.get(url, headers=_headers(ep)))
    r.raise_for_status()
    return r.json()


def _same_model(a: str, b: str) -> bool:
    norm = lambda m: m if ":" in m.rsplit("/", 1)[-1] else m + ":latest"  # noqa: E731
    return norm(a) == norm(b)


async def openwebui_ollama_model(client: httpx.AsyncClient, ep: Endpoint, model: str) -> dict:
    """Open WebUI: the Ollama entry behind a model id ('local.x', or a custom model based on one); {} if none.

    Open WebUI prefixes Ollama model names per connection, so loaded models are matched by digest.
    """
    data = {m.get("id"): m for m in (await _get_json(client, ep, f"{ep.api_base}/models")).get("data", [])}
    m, seen = data.get(model), {model}
    while m and not m.get("ollama"):
        base = (m.get("info") or {}).get("base_model_id")
        if not base or base in seen:
            break
        seen.add(base)
        m = data.get(base)
    return (m or {}).get("ollama") or {}


def find_loaded(ps: dict, model: str, digest: str = "") -> dict | None:
    """Entry of /api/ps for the model (by digest if known, else by name)."""
    return next((m for m in ps.get("models", []) if (digest and m.get("digest") == digest)
                 or _same_model(m.get("name", ""), model) or _same_model(m.get("model", ""), model)), None)


async def context_info(ep: Endpoint, model: str, timeout: float = 10.0) -> dict:
    """Context length the model is actually served with (query after the model has been loaded).

    Keys: context (actual, None = unknown), model_max (Ollama: trained maximum), num_ctx (requested, Ollama),
    others (Ollama: other models loaded at the same time), source, error.
    """
    info: dict = {"context": None, "model_max": None, "num_ctx": ep.ollama_num_ctx or None, "others": [],
                  "source": ""}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if ep.ollama_like:
                try:
                    ps = await _get_json(client, ep, f"{ep.native_base}/api/ps")
                except httpx.HTTPStatusError as e:
                    if e.response.status_code not in (401, 403):
                        raise
                    ps = {}  # Open WebUI: /ollama/api/ps is for admins only
                    info["error"] = "loaded models not visible (Open WebUI: needs an admin API key)"
                owm = await openwebui_ollama_model(client, ep, model) if ep.kind == "openwebui" else {}
                loaded = find_loaded(ps, model, owm.get("digest", ""))
                if loaded:
                    info["context"] = loaded.get("context_length")
                    info["source"] = "ollama /api/ps"
                info["others"] = [m.get("name", "?") for m in ps.get("models", []) if m is not loaded]
                if ep.kind == "openwebui":  # Open WebUI does not pass /api/show through for prefixed models
                    info["model_max"] = (owm.get("details") or {}).get("context_length")
                else:
                    show = await _get_json(client, ep, f"{ep.native_base}/api/show", {"model": model})
                    info["model_max"] = next((v for k, v in (show.get("model_info") or {}).items()
                                              if k.endswith(".context_length")), None)
            else:
                models = await _get_json(client, ep, f"{ep.api_base}/models")
                entry = next((m for m in models.get("data", []) if m.get("id") == model), None)
                if entry and entry.get("max_model_len"):
                    info["context"] = entry["max_model_len"]
                    info["source"] = "/v1/models max_model_len"
        except (httpx.HTTPError, ValueError) as e:
            info["error"] = f"{type(e).__name__}: {e}"
    return info


async def unload_model(ep: Endpoint, model: str, timeout: float = 60.0) -> None:
    """Ollama: remove the model from VRAM now (keep_alive 0) instead of after the idle timeout."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        if ep.kind == "openwebui":  # a custom model: unload the Ollama model it is based on
            model = (await openwebui_ollama_model(client, ep, model)).get("name") or model
        r = await client.post(f"{ep.native_base}/api/generate", json={"model": model, "keep_alive": 0},
                              headers=_headers(ep))
        r.raise_for_status()
