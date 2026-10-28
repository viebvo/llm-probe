"""Make the README screenshots (docs/img/*.png, light and dark) from the example results, against a mock model server.

    .venv/bin/python tools/make_screenshots.py [OUT_DIR]      (default: docs/img)

The server runs on a scratch copy of results.db.sample with an endpoint called "my-server" that points at the mock Ollama
of the GUI regression, so no real server, name or data ends up in a picture. Needs Playwright (see tools/gui_tests/README.md).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "gui_tests"))
from mocks import Mocks  # noqa: E402

WIDTH = 1360
# name -> (tab, viewport height, what to do before the picture)
SHOTS = ["home", "test", "results", "run-details", "compare", "responses"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare(pg, name: str) -> int:
    """Put the page into the state of the picture; returns the viewport height."""
    if name == "home":
        pg.evaluate("showTab('home')")
        return 900
    if name == "test":
        pg.evaluate("showTab('test')"); pg.select_option("#t-ep", "my-server"); pg.wait_for_timeout(3500)
        for m in ("gpt-oss:20b", "qwen3-vl:8b"):
            pg.locator(f"#t-models .mrow[data-model='{m}'] input.mchk").check()
        pg.fill("#t-comment", "after the driver update")
        pg.wait_for_timeout(800)
        return 850
    if name == "results":
        pg.evaluate("showTab('runs')"); pg.wait_for_timeout(1500)
        return 560
    if name == "run-details":
        pg.evaluate("showTab('runs')"); pg.wait_for_timeout(1000); pg.evaluate("selectRun(3)"); pg.wait_for_timeout(3000)
        return 850
    if name == "compare":
        pg.evaluate("setCompare([2,3,5,6])"); pg.evaluate("showTab('cmp')"); pg.wait_for_timeout(4500)
        return 850
    if name == "responses":
        pg.evaluate("setCompare([2,3])"); pg.evaluate("showTab('ans')"); pg.wait_for_timeout(4000)
        return 850
    raise ValueError(name)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "img"
    out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    mocks = Mocks().start()
    home = Path(tempfile.mkdtemp(prefix="llm-probe-shots-"))
    shutil.copy(ROOT / "results.db.sample", home / "results.db")
    (home / "config.json").write_text(json.dumps({"endpoints": [
        {"name": "my-server", "kind": "ollama", "base_url": f"http://127.0.0.1:{mocks.ports['ollama']}"}]}))
    os.chmod(home / "config.json", 0o600)
    port = free_port()
    log = open(home / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "llm_probe", "--server", "--port", str(port)], cwd=ROOT, stdout=log, stderr=log,
                              env={**os.environ, "LLM_PROBE_HOME": str(home)})
    try:
        for _ in range(60):
            with socket.socket() as sk:
                if sk.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.25)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for scheme in ("light", "dark"):
                for name in SHOTS:
                    pg = browser.new_page(viewport={"width": WIDTH, "height": 900}, color_scheme=scheme)
                    pg.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                    pg.evaluate("localStorage.clear()")
                    pg.set_viewport_size({"width": WIDTH, "height": prepare(pg, name)})
                    pg.evaluate("window.scrollTo(0, 0)")
                    pg.screenshot(path=str(out / f"{name}{'-dark' if scheme == 'dark' else ''}.png"))
                    pg.close()
            browser.close()
        print("pictures in", out)
        return 0
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        mocks.stop()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
