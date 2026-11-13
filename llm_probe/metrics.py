"""Extra figures of an endpoint's own backend, read from a URL before and after every request.

An endpoint can list them (`extra_metrics` in config.json, or `--configure NAME --extra-metrics JSON`):

    [{"name": "expert_hits", "url": "/expertstream/api/status", "json_path": "stats.hits", "mode": "delta"},
     {"name": "kv_usage", "url": "/metrics", "prometheus": "vllm:gpu_cache_usage_perc", "mode": "gauge"}]

`url` is a path on the server of the endpoint (or a full http(s) URL); the value comes from a JSON document (`json_path`,
dots, list positions as numbers) or from Prometheus text (`prometheus`, the samples of that metric are added up). `delta` =
after minus before, `gauge` = the value after. A URL that fails only costs a warning; the run goes on.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Endpoint

MODES = ("delta", "gauge")
MAX_METRICS = 8


def clean(raw) -> list[dict]:
    """The usable entries of a list the user wrote (config file, command line); anything else is dropped."""
    out = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        name, url = str(m.get("name") or "").strip(), str(m.get("url") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name) or not url or not (m.get("json_path") or m.get("prometheus")):
            continue
        entry = {"name": name, "url": url, "mode": m.get("mode") if m.get("mode") in MODES else "gauge"}
        entry["json_path" if m.get("json_path") else "prometheus"] = str(m.get("json_path") or m.get("prometheus"))
        out.append(entry)
    return out[:MAX_METRICS]


def _origin(ep: Endpoint) -> str:
    u = urlsplit(ep.base_url)
    return f"{u.scheme}://{u.netloc}/"


def _url(ep: Endpoint, url: str) -> str | None:
    full = urljoin(_origin(ep), url)
    # only the endpoint's own server (a metrics URL in a config file must not send the API key somewhere else)
    return full if urlsplit(full).netloc == urlsplit(ep.base_url).netloc else None


def _json_value(doc, path: str):
    for part in path.split("."):
        if isinstance(doc, list):
            doc = doc[int(part)]
        else:
            doc = doc[part]
    return float(doc)


def _prom_value(text: str, name: str) -> float | None:
    total, found = 0.0, False
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = re.match(rf"{re.escape(name)}(?:\{{[^}}]*\}})?\s+(\S+)", line)
        if m:
            try:
                total += float(m.group(1))
                found = True
            except ValueError:
                continue
    return total if found else None


async def fetch(client: httpx.AsyncClient, ep: Endpoint, warned: set[str] | None = None) -> dict[str, float | None]:
    """name -> current value (None = could not be read). `warned` collects the names that failed (for one warning each)."""
    out: dict[str, float | None] = {}
    headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
    cache: dict[str, httpx.Response | None] = {}
    for m in clean(ep.extra_metrics):
        url = _url(ep, m["url"])
        value = None
        try:
            if url is None:
                raise ValueError("not on the endpoint's server")
            if url not in cache:
                cache[url] = await client.get(url, headers=headers, timeout=3.0)
            r = cache[url]
            r.raise_for_status()
            value = _json_value(r.json(), m["json_path"]) if "json_path" in m else _prom_value(r.text, m["prometheus"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            value = None
        if value is None and warned is not None:
            warned.add(m["name"])
        out[m["name"]] = value
    return out


def combine(ep: Endpoint, before: dict, after: dict) -> dict[str, float]:
    """The figure per metric for one request: after - before (delta) or after (gauge); metrics that could not be read are left out."""
    out = {}
    for m in clean(ep.extra_metrics):
        a, b = after.get(m["name"]), before.get(m["name"])
        if a is None or (m["mode"] == "delta" and b is None):
            continue
        out[m["name"]] = round(a - b, 6) if m["mode"] == "delta" else a
    return out
