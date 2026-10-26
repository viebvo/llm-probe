"""Mock model servers for the GUI regression: no real endpoint is needed.

  ollama  - Ollama look-alike with a curated model list (sizes, quantizations, capabilities)
  vllm    - vLLM look-alike: one model, max_model_len 32768 (num_ctx is read-only there)
  stream  - OpenAI-compatible server that streams fast, long answers (live output, batch tests)
"""
from __future__ import annotations

import asyncio
import json
import random
import threading

from aiohttp import web

OLLAMA_MODELS = {  # name -> (parameter size, quantization, capabilities)
    "qwen2.5:7b": ("7.62B", "Q4_K_M", ["completion", "tools"]),
    "qwen3-vl:8b": ("8.77B", "Q4_K_M", ["completion", "vision", "tools", "thinking"]),
    "gpt-oss:20b": ("20.9B", "MXFP4", ["completion", "tools", "thinking"]),
    "deepseek-r1:14b": ("14.8B", "Q4_K_M", ["completion", "thinking"]),
    "llama3.2:3b": ("3.21B", "Q4_K_M", ["completion", "tools"]),
    "gemma3:12b": ("12.2B", "Q4_K_M", ["completion", "vision"]),
    "smollm:135m": ("134.52M", "Q4_0", ["completion"]),
    "nomic-embed-text:latest": ("137M", "F16", ["embedding"]),
}
WORDS = "the quick brown fox jumps over a lazy dog while thinking about numbers, trees and long sentences".split()


def ollama_app() -> web.Application:
    async def models(_): return web.json_response({"data": [{"id": m} for m in sorted(OLLAMA_MODELS)]})

    async def show(r):
        body = await r.json()
        size, quant, caps = OLLAMA_MODELS.get(body.get("model", ""), ("", "", []))
        return web.json_response({"details": {"parameter_size": size, "quantization_level": quant, "family": "x", "format": "gguf"},
                                  "capabilities": caps, "model_info": {}})

    async def ps(_): return web.json_response({"models": []})

    app = web.Application()
    app.add_routes([web.get("/v1/models", models), web.post("/api/show", show), web.get("/api/ps", ps)])
    return app


def vllm_app() -> web.Application:
    async def models(_): return web.json_response({"data": [{"id": "mock-vllm", "max_model_len": 32768}]})

    app = web.Application()
    app.add_routes([web.get("/v1/models", models)])
    return app


def stream_app(rate: float = 600.0, tokens: int = 3000) -> web.Application:
    """Streams words at `rate` per second, the first third as reasoning: `tokens` of them for the blog-article prompt (long generation, about
    five seconds), 150 for everything else; honours max_tokens (capability probes are short)."""
    async def models(_): return web.json_response({"data": [{"id": "mock-fast"}]})

    async def chat(r):
        body = await r.json()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(r)
        prompt = " ".join(str(m.get("content")) for m in body.get("messages", []))
        n = min(tokens if "blog article" in prompt else 150, body.get("max_tokens") or tokens)  # only the long-generation prompt streams long
        loop = asyncio.get_running_loop()
        t = loop.time()
        try:
            for i in range(n):
                word = random.choice(WORDS) + (" " if i % 17 else "\n")
                key = "reasoning_content" if i < n // 3 else "content"
                await resp.write(f"data: {json.dumps({'choices': [{'delta': {key: word}}]})}\n\n".encode())
                t += 1 / rate
                await asyncio.sleep(max(0, t - loop.time()))
            done = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": n}}
            await resp.write(f"data: {json.dumps(done)}\n\n".encode() + b"data: [DONE]\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    app = web.Application()
    app.add_routes([web.get("/v1/models", models), web.post("/v1/chat/completions", chat)])
    return app


class Mocks:
    """The three mock servers on free ports, served from a background thread."""

    def __init__(self, **stream_args) -> None:
        self.ports: dict[str, int] = {}
        self._apps = {"ollama": ollama_app(), "vllm": vllm_app(), "stream": stream_app(**stream_args)}
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> "Mocks":
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(15)
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def boot() -> None:
            for name, app in self._apps.items():
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                self.ports[name] = site._server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
