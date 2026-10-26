"""Would two tests running at the same time affect each other?

Checked from what can be known: the same server (URL), the same machine (host names resolving to the same
address) and the same GPUs (UUIDs from nvidia-smi via SSH - unique per physical card, so containers sharing a
GPU are recognised even with different addresses).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import asyncssh

from .config import Endpoint
from .monitor import _connect_kwargs

YES, NO, MAYBE = "yes", "no", "maybe"


def _server(ep: Endpoint) -> tuple[str, int]:
    u = urlsplit(ep.base_url if "://" in ep.base_url else "http://" + ep.base_url)
    return (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


async def _addresses(host: str) -> set[str]:
    if not host:
        return set()
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None), 5)
    except (OSError, asyncio.TimeoutError):
        return set()
    addrs = {info[4][0] for info in infos}
    if any(a.startswith("127.") or a == "::1" for a in addrs):
        addrs.add("localhost")
    return addrs


async def public_host(ep: Endpoint) -> tuple[bool | None, str]:
    """(whether the endpoint's server has a public internet address, its host name); None = not resolvable."""
    host = _server(ep)[0]
    addrs = await _addresses(host)
    addrs.discard("localhost")
    if not addrs:
        return None, host
    return any(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs), host


async def gpu_uuids(ep: Endpoint) -> tuple[set[str] | None, str]:
    """UUIDs of the GPUs of the endpoint's machine via SSH, or None and why not."""
    if not ep.ssh_host:
        return None, f"{ep.name} has no SSH host set"
    try:
        async with asyncssh.connect(**(_connect_kwargs(ep) | {"connect_timeout": 6})) as conn:
            r = await asyncio.wait_for(conn.run("nvidia-smi --query-gpu=uuid --format=csv,noheader", check=False), 8)
    except (OSError, asyncssh.Error, asyncio.TimeoutError):
        return None, f"{ep.name} is not reachable via SSH"
    uuids = {line.strip() for line in str(r.stdout).splitlines() if line.strip().startswith("GPU-")}
    return (uuids, "") if uuids else (None, f"nvidia-smi on {ep.name} reports no GPU")


async def assess(new: Endpoint, running: Endpoint | None) -> tuple[str, str]:
    """(YES | NO | MAYBE, reason) - whether a test of `new` would affect one running on `running`."""
    if running is None:
        return MAYBE, "the endpoint of the running test is not in the settings any more, so it cannot be checked"
    a, b = _server(new), _server(running)
    if a == b:
        return YES, f"both use the same server ({a[0]}:{a[1]})"
    (gpus_new, why_new), (gpus_running, why_running) = await asyncio.gather(gpu_uuids(new), gpu_uuids(running))
    if gpus_new and gpus_running and gpus_new & gpus_running:
        n = len(gpus_new & gpus_running)
        return YES, f"both servers use the same GPU{'s' if n > 1 else ''} ({n} shared, found via SSH)"
    same_machine = bool((await _addresses(a[0])) & (await _addresses(b[0])))
    if same_machine:
        if gpus_new and gpus_running:
            return MAYBE, (f"both servers run on the same machine ({a[0]} / {b[0]}) - on different GPUs, but they "
                           "share CPU, memory and the PCIe bus")
        return YES, f"both servers run on the same machine ({a[0]} / {b[0]})"
    if gpus_new and gpus_running:
        return NO, "different machines with different GPUs (checked via SSH)"
    why = "; ".join(w for w in (why_new, why_running) if w)
    return MAYBE, (f"different addresses, but whether they share GPUs cannot be checked ({why}) - containers or VMs "
                   "on the same host can share them")


QUESTIONS = {
    YES: ("They WILL affect each other: {reason}. Both would share the server or the GPUs, and the measurements of "
          "both would be distorted.\n\nStart anyway? Not recommended."),
    NO: ("They will not affect each other: {reason}. Only the machine running LLM Probe is shared, which hardly "
         "matters.\n\nStart it now?"),
    MAYBE: ("They MAY affect each other: {reason}. If they share hardware, the measurements of both would be "
            "distorted.\n\nStart anyway?"),
}


def question(level: str, reason: str, what: str) -> str:
    """Text of the start dialog after holder_text()."""
    return (f"Your {what} has not been started yet.\n\n" + QUESTIONS[level].format(reason=reason) +
            "\n\nOtherwise wait until the other one is finished, or stop it there with Abort (if that process "
            "hangs, end it - its lock is released automatically).")
