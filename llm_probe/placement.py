"""How the model was loaded: CPU/GPU split, GPUs used and the server's load parameters.

Collected once per run after the warmup, from the API (Ollama /api/ps + /api/show, vLLM /metrics) and,
if an SSH host is configured, from the server (nvidia-smi per process, command line of the model process).
"""

from __future__ import annotations

import re

import asyncssh
import httpx

from .client import find_loaded, openwebui_ollama_model
from .config import Endpoint
from .monitor import _connect_kwargs

# Runs remotely once: GPUs, GPU processes and the command lines of the processes that may serve the model.
REMOTE_PROBE = r"""
echo "@@GPU"
nvidia-smi --query-gpu=index,uuid --format=csv,noheader 2>/dev/null
echo "@@APP"
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null
echo "@@PROC"
for p in $( { nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null
              pgrep -f 'llama-server|ollama runner|vllm' ; } | sort -u ); do
  [ -r /proc/$p/cmdline ] || continue
  printf '%s\t%s\t' "$p" "$(awk '/^VmRSS/ {print $2}' /proc/$p/status 2>/dev/null)"
  tr '\0' ' ' < /proc/$p/cmdline; echo
done
"""

# command line flag -> parameter name (Ollama's llama-server / "ollama runner", vLLM)
FLAGS = {
    "-c": "ctx", "--ctx-size": "ctx",
    "-np": "parallel", "--parallel": "parallel",
    "-ngl": "gpu_layers", "--n-gpu-layers": "gpu_layers", "--gpu-layers": "gpu_layers",
    "-ts": "tensor_split", "--tensor-split": "tensor_split",
    "-ctk": "kv_k", "--cache-type-k": "kv_k", "-ctv": "kv_v", "--cache-type-v": "kv_v", "--kv-cache-type": "kv",
    "-fa": "flash_attn", "--flash-attn": "flash_attn",
    "-b": "batch", "--batch-size": "batch", "-ub": "ubatch", "--ubatch-size": "ubatch",
    "-t": "threads", "--threads": "threads",
    "--no-mmap": "no_mmap", "--mlock": "mlock",
    "-tp": "tp", "--tensor-parallel-size": "tp", "-pp": "pp", "--pipeline-parallel-size": "pp",
    "-q": "quant", "--quantization": "quant", "--dtype": "dtype", "--kv-cache-dtype": "kv",
    "--cpu-offload-gb": "cpu_offload_gb", "--swap-space": "swap_space",
    "--max-model-len": "max_model_len", "--max-num-seqs": "max_num_seqs",
    "--gpu-memory-utilization": "gpu_mem_util", "--enforce-eager": "enforce_eager",
}

# vllm:cache_config_info labels worth keeping
VLLM_CACHE_KEYS = ("gpu_memory_utilization", "num_gpu_blocks", "num_cpu_blocks", "block_size", "cache_dtype",
                   "enable_prefix_caching", "kv_offloading_size", "cpu_offload_gb", "swap_space")

_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_args(cmdline: str) -> dict[str, str]:
    """Known flags of a model server command line; bare flags get the value 'on'."""
    tokens = cmdline.split()
    out: dict[str, str] = {}
    for i, tok in enumerate(tokens):
        flag, eq, value = tok.partition("=")
        if flag not in FLAGS:
            continue
        if not eq:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            value = nxt if nxt and not (nxt.startswith("-") and not nxt[1:2].isdigit()) else "on"
        out[FLAGS[flag]] = value
    return out


def parse_metrics(text: str) -> tuple[dict[str, str], float | None]:
    """vLLM /metrics -> (cache_config_info labels, KV cache usage 0..1)."""
    cache: dict[str, str] = {}
    usage = None
    for line in text.splitlines():
        if line.startswith("vllm:cache_config_info{"):
            cache = {k: v for k, v in _LABEL_RE.findall(line) if k in VLLM_CACHE_KEYS}
        elif line.startswith(("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")):
            try:
                v = float(line.rsplit(" ", 1)[1])
            except ValueError:
                continue
            usage = v if usage is None else max(usage, v)
    return cache, usage


async def vllm_metrics(ep: Endpoint, timeout: float = 5.0) -> tuple[dict[str, str], float | None]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{ep.native_base}/metrics")
        r.raise_for_status()
        return parse_metrics(r.text)


async def _api(ep: Endpoint, model: str, errors: list[str]) -> dict:
    out: dict = {}
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        if ep.ollama_like:
            r = await client.get(f"{ep.native_base}/api/ps")
            if r.status_code in (401, 403):  # Open WebUI passes /api/ps through for admins only
                errors.append("API: CPU/GPU split not visible - Open WebUI needs an admin API key for /ollama/api/ps")
            else:
                r.raise_for_status()
                owm = await openwebui_ollama_model(client, ep, model) if ep.kind == "openwebui" else {}
                if loaded := find_loaded(r.json(), model, owm.get("digest", "")):
                    out["size"], out["size_vram"] = loaded.get("size"), loaded.get("size_vram")
            if ep.kind == "openwebui":
                return out  # /api/show is not passed through for prefixed names; no blob -> SSH matching by count
            r = await client.post(f"{ep.native_base}/api/show", json={"model": model})
            show = r.json() if r.status_code == 200 else {}
            if m := re.search(r"^FROM (\S*sha256-[0-9a-f]+)", show.get("modelfile", "") if isinstance(show, dict)
                              else "", re.M):
                out["blob"] = m.group(1).rsplit("/", 1)[-1]  # identifies the runner process of this model
        elif ep.kind == "vllm":
            cache, _ = await vllm_metrics(ep)
            out["cache_config"] = cache
    return out


async def _ssh(ep: Endpoint, kind: str, blob: str | None) -> dict:
    async with asyncssh.connect(**_connect_kwargs(ep)) as conn:
        r = await conn.run(REMOTE_PROBE, check=False)
    section, uuid_idx, apps, procs = "", {}, [], {}
    for line in str(r.stdout).splitlines():
        if line.startswith("@@"):
            section = line
            continue
        if section == "@@GPU" and "," in line:
            idx, uuid = [x.strip() for x in line.split(",", 1)]
            uuid_idx[uuid] = int(idx)
        elif section == "@@APP" and line.count(",") >= 2:
            uuid, pid, mib = [x.strip() for x in line.split(",")[:3]]
            if uuid in uuid_idx and pid.isdigit():
                # None: not reported (unified memory: "[N/A]")
                apps.append((uuid_idx[uuid], int(pid), float(mib) if mib.replace(".", "").isdigit() else None))
        elif section == "@@PROC" and line.count("\t") >= 2:
            pid, rss, args = line.split("\t", 2)
            if "@@PROC" not in args:  # not the probe script itself
                procs[int(pid)] = {"rss_mb": int(rss) / 1024 if rss.isdigit() else None, "args": args.strip()}

    out: dict = {}
    if kind in ("ollama", "openwebui"):
        runners = {p: v for p, v in procs.items() if "llama-server" in v["args"] or " runner" in v["args"]}
        mine = [p for p, v in runners.items() if blob and blob in v["args"]] or (
            list(runners) if len(runners) == 1 else [])
        if not mine:
            if runners:
                out["note"] = "several model processes, none matched"
            elif kind == "openwebui":
                out["note"] = "no Ollama process on this host - enter the Ollama host as SSH host of this endpoint"
            else:
                out["note"] = "model process not found"
        pids = set(mine)
        server = procs[mine[0]] if mine else None
    else:
        server = next((v for v in procs.values() if re.search(r"vllm(\s+serve|\.entrypoints)", v["args"])), None)
        pids = set(procs)  # vLLM runs its workers as separate processes; count all GPU processes of the server
    per_gpu: dict[int, float | None] = {}
    for idx, pid, mib in apps:
        if pid in pids:
            per_gpu[idx] = None if mib is None or per_gpu.get(idx, 0.0) is None else per_gpu.get(idx, 0.0) + mib
    out["gpus"] = [{"index": i, "mib": per_gpu[i]} for i in sorted(per_gpu)]
    if server:
        out["args"] = parse_args(server["args"])
        if kind in ("ollama", "openwebui"):
            out["rss_mb"] = server["rss_mb"]
    return out


async def collect(ep: Endpoint, model: str) -> dict:
    """Keys: api (see _api), ssh (see _ssh, only with an SSH host), errors."""
    info: dict = {"kind": ep.kind, "api": {}, "errors": []}
    try:
        info["api"] = await _api(ep, model, info["errors"])
    except (httpx.HTTPError, ValueError) as e:
        info["errors"].append(f"API: {type(e).__name__}: {e}")
    if ep.ssh_host:
        try:
            info["ssh"] = await _ssh(ep, ep.kind, info["api"].get("blob"))
        except (OSError, asyncssh.Error) as e:
            info["errors"].append(f"SSH: {type(e).__name__}: {e}")
    return info


# ------------------------------------------------------------------- Labels

def _offload_gb(p: dict) -> float | None:
    """vLLM weight offload to the CPU in GB; None = not known (no SSH and not in the metrics)."""
    args = p.get("ssh", {}).get("args")
    value = (args or {}).get("cpu_offload_gb", p.get("api", {}).get("cache_config", {}).get("cpu_offload_gb"))
    if value is None and args is not None and p.get("kind") == "vllm":
        value = "0"  # command line known, flag not set
    try:
        return None if value is None else float(value)
    except ValueError:
        return None


def cpu_share(p: dict) -> float | None:
    """Share of the model in system RAM (0..1); None = unknown (vLLM with offload: only GB are known)."""
    api = p.get("api", {})
    if api.get("size") and api.get("size_vram") is not None:
        return max(0.0, 1 - api["size_vram"] / api["size"])
    return 0.0 if _offload_gb(p) == 0 else None


def label(p: dict | None) -> str:
    """Short placement, e.g. '100% GPU · 2 GPUs 5.2+5.4 GB' or '3%/97% CPU/GPU'."""
    if not p:
        return "-"
    parts = []
    share, offload = cpu_share(p), _offload_gb(p)
    if p.get("api", {}).get("size") and share is not None:
        cpu = round(share * 100)
        parts.append("100% GPU" if cpu == 0 else "100% CPU" if cpu == 100 else f"{cpu}%/{100 - cpu}% CPU/GPU")
    elif offload:
        parts.append(f"GPU + {offload:g} GB CPU offload")
    elif p.get("kind") == "vllm":
        parts.append("GPU" if offload == 0 else "GPU (offload not checked)")
    gpus = p.get("ssh", {}).get("gpus") or []
    if any(g["mib"] is None for g in gpus):  # unified memory: no memory per GPU process reported
        parts.append(f"GPU{gpus[0]['index']} (shared memory)" if len(gpus) == 1 else f"{len(gpus)} GPUs (shared memory)")
    elif len(gpus) > 1:
        parts.append(f"{len(gpus)} GPUs " + "+".join(f"{g['mib'] / 1024:.1f}" for g in gpus) + " GB")
    elif gpus:
        parts.append(f"GPU{gpus[0]['index']} {gpus[0]['mib'] / 1024:.1f} GB")
    return " · ".join(parts) or "-"


def needs_attention(p: dict | None) -> bool:
    """Part of the model runs on the CPU."""
    if not p:
        return False
    share = cpu_share(p)
    return bool(share and share >= 0.005) or bool(_offload_gb(p))


PARAM_NAMES = [  # (key, display name) in display order
    ("parallel", "parallel"), ("ctx", "ctx total"), ("gpu_layers", "GPU layers"), ("tensor_split", "tensor split"),
    ("kv", "KV cache"), ("kv_k", "KV K"), ("kv_v", "KV V"), ("flash_attn", "flash-attn"), ("batch", "batch"),
    ("ubatch", "ubatch"), ("threads", "threads"), ("no_mmap", "no-mmap"), ("mlock", "mlock"),
    ("tp", "TP"), ("pp", "PP"), ("quant", "quant"), ("dtype", "dtype"), ("cpu_offload_gb", "CPU offload GB"),
    ("swap_space", "swap GB"), ("max_model_len", "max len"), ("max_num_seqs", "max seqs"),
    ("gpu_mem_util", "GPU mem util"), ("enforce_eager", "eager"),
    ("gpu_memory_utilization", "GPU mem util"), ("num_gpu_blocks", "GPU KV blocks"),
    ("num_cpu_blocks", "CPU KV blocks"), ("cache_dtype", "KV cache"), ("enable_prefix_caching", "prefix cache"),
    ("kv_offloading_size", "KV offload GB"),
]


def params(p: dict | None) -> dict[str, str]:
    """Load parameters by display name (command line wins over the vLLM cache config)."""
    if not p:
        return {}
    raw = dict(p.get("api", {}).get("cache_config", {})) | dict(p.get("ssh", {}).get("args", {}))
    out: dict[str, str] = {}
    for key, name in PARAM_NAMES:
        if raw.get(key) not in (None, "", "None") and name not in out:
            out[name] = str(raw[key])
    return out


def params_label(p: dict | None) -> str:
    ps = params(p)
    if not p:
        return "-"
    text = " · ".join(f"{k} {v}" for k, v in ps.items())
    rss = p.get("ssh", {}).get("rss_mb")
    if rss:
        text += f"{' · ' if text else ''}process RAM {rss / 1024:.1f} GB"
    if not text:
        text = "no SSH host - only the API was queried" if "ssh" not in p else "-"
    return text


def mismatch(a: dict | None, b: dict | None, names: tuple[str, str] = ("A", "B")) -> list[str]:
    """Differences between two runs that make speed comparisons questionable."""
    if not a or not b:
        return []
    out = []
    na, nb = names
    sa, sb = cpu_share(a), cpu_share(b)
    if (sa is not None and sb is not None and abs(sa - sb) >= 0.01) or needs_attention(a) != needs_attention(b):
        out.append(f"placement differs ({na} {label(a)}, {nb} {label(b)})")
    pa, pb = params(a), params(b)
    diff = [k for k in dict.fromkeys(list(pa) + list(pb)) if k in pa and k in pb and pa[k] != pb[k]]
    if diff:
        out.append("server parameters differ: " + ", ".join(f"{k} {pa[k]} vs. {pb[k]}" for k in diff))
    return out
