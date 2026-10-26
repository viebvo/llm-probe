"""Resource monitoring of the LLM server via SSH (nvidia-smi, /proc/stat, /proc/meminfo)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import asyncssh

from .config import Endpoint

# Runs remotely as an endless loop; each sample is enclosed in @@T ... @@E.
REMOTE_LOOP = r"""
while true; do
  echo "@@T"
  nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu \
    --format=csv,noheader,nounits 2>/dev/null | sed 's/^/@@G /'
  head -1 /proc/stat | sed 's/^/@@C /'
  grep -E '^(MemTotal|MemAvailable):' /proc/meminfo | sed 's/^/@@M /'
  echo "@@E"
  sleep {interval}
done
"""


@dataclass
class GpuSample:
    index: int
    name: str
    util: float
    mem_used: float  # MiB
    mem_total: float
    power: float | None  # W
    temp: float | None
    shared: bool = False  # no own memory reported (unified CPU/GPU memory): mem_* are the system RAM


@dataclass
class Sample:
    t: float  # local time.monotonic()
    gpus: list[GpuSample] = field(default_factory=list)
    cpu_pct: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None  # e.g. "[N/A]"


def _connect_kwargs(ep: Endpoint) -> dict:
    kw: dict = {
        "host": ep.ssh_host,
        "port": ep.ssh_port,
        "known_hosts": None,  # servers on the local LAN; host keys change when a VM/container is rebuilt
        "connect_timeout": 10,
    }
    if ep.ssh_user:  # empty = local user name
        kw["username"] = ep.ssh_user
    if ep.ssh_password:
        kw["password"] = ep.ssh_password
    if ep.ssh_key_file:
        kw["client_keys"] = [ep.ssh_key_file]
    return kw


async def test_ssh(ep: Endpoint) -> str:
    async with asyncssh.connect(**_connect_kwargs(ep)) as conn:
        r = await conn.run("hostname; nvidia-smi -L 2>&1 || echo 'nvidia-smi not found'", check=False)
        return str(r.stdout).strip()


class ResourceMonitor:
    """Collects samples in the background while the benchmark is running."""

    def __init__(self, ep: Endpoint, interval: float = 1.0):
        self.ep = ep
        self.interval = interval
        self.samples: list[Sample] = []
        self.error: str = ""
        self._task: asyncio.Task | None = None
        self._prev_cpu: tuple[int, int] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.ep.ssh_host)

    @property
    def latest(self) -> Sample | None:
        return self.samples[-1] if self.samples else None

    async def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        try:
            async with asyncssh.connect(**_connect_kwargs(self.ep)) as conn:
                proc = await conn.create_process(REMOTE_LOOP.format(interval=self.interval))
                cur: Sample | None = None
                async for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line == "@@T":
                        cur = Sample(t=time.monotonic())
                    elif cur is None:
                        continue
                    elif line.startswith("@@G "):
                        f = [x.strip() for x in line[4:].split(",")]
                        if len(f) >= 7:
                            # unified memory (CPU and GPU share the RAM): nvidia-smi reports "[N/A]" or
                            # "[Not Supported]" - filled in with the system RAM at the end of the sample
                            shared = _num(f[4]) is None
                            cur.gpus.append(GpuSample(int(f[0]), f[1], _num(f[2]) or 0, _num(f[3]) or 0,
                                                      _num(f[4]) or 0, _num(f[5]), _num(f[6]), shared))
                    elif line.startswith("@@C "):
                        vals = [int(x) for x in line[4:].split()[1:]]
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        total = sum(vals[:8])
                        if self._prev_cpu:
                            dt, di = total - self._prev_cpu[0], idle - self._prev_cpu[1]
                            cur.cpu_pct = 100.0 * (dt - di) / dt if dt > 0 else 0.0
                        self._prev_cpu = (total, idle)
                    elif line.startswith("@@M "):
                        key, val = line[4:].split(":", 1)
                        mb = int(val.split()[0]) / 1024
                        if key == "MemTotal":
                            cur.mem_total_mb = mb
                        else:
                            cur.mem_used_mb = (cur.mem_total_mb or 0) - mb
                    elif line == "@@E":
                        for g in cur.gpus:
                            if g.shared:
                                g.mem_used, g.mem_total = cur.mem_used_mb or 0, cur.mem_total_mb or 0
                        self.samples.append(cur)
                        cur = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # SSH errors must not abort the benchmark
            self.error = f"{type(e).__name__}: {e}"
