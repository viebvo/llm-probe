"""Configuration (endpoints, benchmark settings) stored as a JSON file."""

from __future__ import annotations

import json
import re
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterator

def _default_data_dir() -> Path:
    """data/ in the project folder when started from it (run.sh / run.bat); installed as a package (e.g. with
    pipx) the per-user data folder of the system."""
    root = Path(__file__).resolve().parent.parent
    if (root / "run.sh").exists() or (root / "data").is_dir():
        return root / "data"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "llm-probe"


DATA_DIR = Path(os.environ.get("LLM_PROBE_HOME") or _default_data_dir())
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "results.db"
PROMPTS_FILE = DATA_DIR / "prompts.json"

DEFAULT_PORTS = {"ollama": 11434, "vllm": 8000}
# reasoning switch sent with every request: "" = model default (nothing sent), off, or an effort level.
# Effort names differ per server (OpenAI: low/medium/high; some vLLM models add xhigh / minimal); the capability
# check probes which ones a given endpoint actually accepts.
THINKING_MODES = ["", "off", "low", "medium", "high", "xhigh"]  # what the Test tab offers for every model
# any other name a model accepts (found by the Q query, e.g. "minimal", "ultra", "max") is valid, too
THINKING_NAME = re.compile(r"[a-z0-9_-]{1,24}")


def clean_currency(text) -> str:
    """The unit of the prices as shown next to them: at most 8 characters, no whitespace and nothing that is markup."""
    return re.sub(r"[\s<>&\"'`]", "", str(text or ""))[:8]


def valid_thinking(mode) -> bool:
    return isinstance(mode, str) and (mode == "" or bool(THINKING_NAME.fullmatch(mode)))


@dataclass
class Endpoint:
    name: str
    kind: str = "ollama"  # "ollama" | "vllm" | "openwebui" | "openai"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    model: str = ""  # last tested model
    models: list[str] = field(default_factory=list)  # last selection in the model list (batch)
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_file: str = ""
    num_ctx: int = 0  # Ollama (also behind Open WebUI): context length to load the model with (0 = server default)
    price_in: float = 0.0  # optional price per 1 million input tokens (any currency), for cost estimates
    price_out: float = 0.0  # ... per 1 million output tokens
    currency: str = ""  # unit of the prices, shown next to them and with the costs (free text, e.g. EUR, USD, $)

    @property
    def ollama_like(self) -> bool:
        """Ollama's native API is available (directly, or via Open WebUI's /ollama passthrough)."""
        return self.kind in ("ollama", "openwebui")

    def _root(self, suffix: str) -> str:
        url = self.base_url.rstrip("/")
        return url[: -len(suffix)] if url.endswith(suffix) else url

    @property
    def api_base(self) -> str:
        """Base URL of the OpenAI-compatible API: .../v1, for Open WebUI .../api."""
        if self.kind == "openwebui":
            return self._root("/api") + "/api"
        return self._root("/v1") + "/v1"

    @property
    def selected_models(self) -> list[str]:
        return self.models or ([self.model] if self.model else [])

    @property
    def native_base(self) -> str:
        """Base URL of Ollama's native API (/api/...) and vLLM's /metrics; Open WebUI proxies Ollama at /ollama."""
        if self.kind == "openwebui":
            return self._root("/api") + "/ollama"
        return self._root("/v1")

    @property
    def ollama_num_ctx(self) -> int:
        """num_ctx to send, 0 = do not set (OpenAI-compatible API, server default)."""
        return self.num_ctx if self.ollama_like and self.num_ctx > 0 else 0


@dataclass
class BenchSettings:
    max_tokens: int = 32768  # a cap, not a target: short answers stop early; reasoning models need the headroom
    temperature: float = 1.0  # many models loop endlessly at 0; 1.0 is the safer trade-off across endpoints
    repetitions: int = 3
    concurrency_levels: list[int] = field(default_factory=lambda: [1, 2, 4, 8])
    requests_per_level: int = 2  # requests per slot and level (total = level * value)
    request_timeout: float = 300.0
    monitor_interval: float = 1.0
    run_single: bool = True
    run_concurrency: bool = True
    thinking: str = ""  # one of THINKING_MODES
    escape_word: str = ""  # the word that tells the model it may say "I cannot": "" = "unsolvable", "random" = a new code word per run
    # a batch run: several values per setting; each combination is its own run (see batch_plan). Empty = just the
    # scalar above. The scalars stay what every stored run, the CLI and the results columns know.
    max_tokens_values: list[int] = field(default_factory=list)
    temperature_values: list[float] = field(default_factory=list)
    thinking_values: list[str] = field(default_factory=list)
    num_ctx_values: list[int] = field(default_factory=list)  # Ollama: context length(s) to load the model with; one value overrides the endpoint's, several make a batch
    request_timeout_values: list[float] = field(default_factory=list)


def split_values(text) -> list[str]:
    """'2048, 8192' / ['a', 'b'] -> ['2048', '8192'] (comma separated, blanks dropped, duplicates removed in order)."""
    items = text if isinstance(text, (list, tuple)) else str(text if text is not None else "").replace(";", ",").split(",")
    return list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))


def parse_values(text, cast, label: str, minimum: float | None = None) -> list:
    """The values of a comma separated setting as numbers; ValueError with a readable message otherwise."""
    out = []
    for item in split_values(text):
        try:
            v = cast(item)
        except ValueError:
            raise ValueError(f"{label}: '{item}' is not a number") from None
        if minimum is not None and v < minimum:
            raise ValueError(f"{label}: {item} is below {minimum:g}")
        out.append(v)
    return out


MAX_BATCH_RUNS = 100


def batch_plan(ep: Endpoint, s: BenchSettings) -> list[dict]:
    """The runs of ONE model that the multi-valued settings span, in the order they run: per num_ctx (the model
    reloads once per value), then thinking, max_tokens, temperature, timeout. Each entry: the scalar settings of that run,
    its endpoint (num_ctx applied) and a note naming the values that differ between the runs."""
    num_ctx = s.num_ctx_values if ep.ollama_like and s.num_ctx_values else [None]
    think = s.thinking_values or [s.thinking]
    tokens = s.max_tokens_values or [s.max_tokens]
    temps = s.temperature_values or [s.temperature]
    timeouts = s.request_timeout_values or [s.request_timeout]
    combos = [(c, th, mt, t, to) for c in num_ctx for th in think for mt in tokens for t in temps for to in timeouts]
    out = []
    for i, (c, th, mt, t, to) in enumerate(combos, 1):
        parts = [f"max_tokens {mt}" if len(tokens) > 1 else "", f"temp {t:g}" if len(temps) > 1 else "",
                 f"thinking {th or 'default'}" if len(think) > 1 else "", f"num_ctx {c}" if c and len(num_ctx) > 1 else "",
                 f"timeout {to:g} s" if len(timeouts) > 1 else ""]
        note = f"batch {i}/{len(combos)} · " + " · ".join(p for p in parts if p) if len(combos) > 1 else ""
        out.append({"settings": replace(s, max_tokens=mt, temperature=t, thinking=th, request_timeout=to, max_tokens_values=[],
                                        temperature_values=[], thinking_values=[], num_ctx_values=[], request_timeout_values=[]),
                    "endpoint": replace(ep, num_ctx=c) if c else ep, "note": note})
    return out


@dataclass
class Config:
    endpoints: list[Endpoint] = field(default_factory=list)
    bench: BenchSettings = field(default_factory=BenchSettings)
    # Test tab: per-model overrides of the Whole test settings, {endpoint: {model: {setting: value}}}; a setting that is
    # not there is "like whole test"
    model_params: dict = field(default_factory=dict)
    # Test tab: what the reasoning query of a model found, {endpoint: {model: {reasons, efforts, checked, note}}}
    reasoning: dict = field(default_factory=dict)

    def endpoint(self, name: str) -> Endpoint | None:
        return next((e for e in self.endpoints if e.name == name), None)


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        return Config()
    raw = json.loads(CONFIG_FILE.read_text())
    endpoint_fields = Endpoint.__dataclass_fields__
    bench_fields = BenchSettings.__dataclass_fields__
    return Config(
        endpoints=[Endpoint(**{k: v for k, v in e.items() if k in endpoint_fields}) for e in raw.get("endpoints", [])],
        bench=BenchSettings(**{k: v for k, v in raw.get("bench", {}).items() if k in bench_fields}),
        model_params=raw.get("model_params") or {},
        reasoning=raw.get("reasoning") or {},
    )


def save_config(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    # file may contain SSH passwords/API keys -> readable by owner only
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(cfg), f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


@contextmanager
def edit_config() -> Iterator[Config]:
    """config.json as stored right now, to change; saved afterwards. Under a lock shared by all processes, so the
    web UI and command line do not overwrite each other's changes."""
    from .lock import file_lock
    with file_lock(CONFIG_FILE):
        cfg = load_config()
        yield cfg
        save_config(cfg)
