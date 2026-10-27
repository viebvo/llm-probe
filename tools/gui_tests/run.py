#!/usr/bin/env python3
"""GUI regression for LLM Probe: drives every tab of the web UI with Playwright against mock model servers and a copy of
results.db.sample. No real endpoint, no real data. Run it after every build:

    .venv/bin/python tools/gui_tests/run.py            # everything
    .venv/bin/python tools/gui_tests/run.py --only results,test_tab
    .venv/bin/python tools/gui_tests/run.py --update-baseline   # accept the current numbers of the sample database

Needs: pip install playwright && playwright install chromium. Exit code 0 = all checks passed.
Every bug found in the GUI deserves one more check here (see README.md).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from mocks import Mocks  # noqa: E402

from llm_probe import __version__  # noqa: E402

FAKE_KEY = "SECRET-TEST-KEY-1234"  # planted in the config: it must never reach the browser
TABS = ["home", "test", "runs", "cmp", "ans", "prompts", "ep"]
NO_TIP_OK = {"ans-grid"}  # data-tip keys whose text is empty on purpose


class Suite:
    def __init__(self, base: str, home: Path, mocks: Mocks, browser) -> None:
        self.base, self.home, self.mocks, self.browser = base, home, mocks, browser
        self.results: list[tuple[bool, str]] = []
        self.group = ""

    def ok(self, name: str, cond, detail="") -> bool:
        cond = bool(cond)
        self.results.append((cond, f"{self.group}: {name}"))
        print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"   <- {str(detail)[:300]}"), flush=True)
        return cond

    def page(self, scheme="light", width=1500, height=1100):
        pg = self.browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        pg.goto(self.base, wait_until="networkidle")
        pg.evaluate("localStorage.clear()")
        return pg

    def get(self, path: str, headers: dict | None = None, method="GET", body=None):
        host, port = self.base.split("//")[1].split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=15)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read().decode(errors="replace")


# ------------------------------------------------------------------------------------------------ the checks
def check_tour(s: Suite) -> None:
    """Every tab in both themes: no script errors, the version, and a tooltip text behind every data-tip."""
    for scheme in ("light", "dark"):
        pg = s.page(scheme)
        for tab in TABS:
            pg.evaluate(f"showTab('{tab}')")
            pg.wait_for_timeout(500)
        s.ok(f"{scheme}: all tabs open without script errors", not pg.errors, pg.errors[:2])
        bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        s.ok(f"{scheme}: body background set", bg not in ("rgba(0, 0, 0, 0)", ""), bg)
        if scheme == "light":
            s.ok("version in the header", __version__ in pg.inner_text("#app-version"), pg.inner_text("#app-version"))
            missing = pg.evaluate("[...document.querySelectorAll('[data-tip]')].filter(e => !e.title).map(e => e.dataset.tip)")
            s.ok("every data-tip has a tooltip text", not set(missing) - NO_TIP_OK, sorted(set(missing) - NO_TIP_OK)[:8])
        pg.close()


def check_home(s: Suite) -> None:
    pg = s.page()
    t = pg.inner_text("#tab-home")
    s.ok("TL;DR lines on the Home tab", t.count("TL;DR") >= 6, t.count("TL;DR"))
    s.ok("tile 2 wording", "the quality of the response (quality: A wrong answer nobody notices" in t)
    pg.click("text=Go to Test"); pg.wait_for_timeout(300)
    s.ok("'Go to Test' opens the Test tab", pg.evaluate("currentTab") == "test", pg.evaluate("currentTab"))
    pg.close()


def check_results(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("showTab('runs')"); pg.wait_for_timeout(1200)
    rows = pg.evaluate("document.querySelectorAll('#runs-table tbody tr').length")
    s.ok("the six sample runs are listed", rows == 6, rows)
    counts = []
    for _ in range(4):
        pg.click("#r-mark-all"); pg.wait_for_timeout(150)
        counts.append(pg.evaluate("document.querySelectorAll('#runs-table tbody input[type=checkbox]:checked').length"))
    s.ok("header check box rotates (all first, a different state each click)", counts[0] == 6 and len(set(counts[:3])) >= 2, counts)
    while pg.evaluate("document.querySelectorAll('#runs-table tbody input[type=checkbox]:checked').length"):
        pg.click("#r-mark-all"); pg.wait_for_timeout(100)
        if pg.evaluate("state.marked.size") == 6: pg.click("#r-mark-all")
        pg.wait_for_timeout(100)
        break
    pg.evaluate("state.marked.clear(); loadRuns()"); pg.wait_for_timeout(500)
    dirs = []
    for _ in range(3):
        pg.click("#runs-table th[data-sort='model']"); pg.wait_for_timeout(250)
        dirs.append(pg.evaluate("document.querySelector(\"#runs-table th[data-sort='model']\").dataset.dir"))
    s.ok("sort rotates ascending, descending, original", dirs == ["asc", "desc", ""], dirs)
    pg.check("#r-params"); pg.wait_for_timeout(300)
    s.ok("'Test parameters' shows the parameter columns", pg.evaluate("getComputedStyle(document.querySelector('#runs-table .param-col')).display") != "none")
    pg.uncheck("#r-params")
    s.ok("nothing marked: export / compare marked disabled", pg.evaluate("$('r-export').disabled && $('r-cmp-marked').disabled"))
    pg.evaluate("selectRunNow(3)"); pg.wait_for_timeout(3000)
    d = pg.inner_text("#run-detail")
    s.ok("run details open", "Run #3" in d)
    s.ok("thinking says what was asked and what came of it", "thinking model default -> " in d, [x for x in d.split("\n") if "thinking" in x][:1])
    s.ok("Cost carries the currency", "EUR" in d)
    s.ok("quality/reliability tiles have 'show relevant prompts'", pg.evaluate("document.querySelectorAll('.tilebtn button').length") >= 7)
    leg = pg.evaluate("[...document.querySelectorAll('#run-detail .sc-legend span')].map(x => x.textContent.trim())")
    s.ok("scaling legend carries the units", "Throughput (tok/s)" in leg and "p95 latency (sec)" in leg, leg)
    h = pg.evaluate("[scalingCard('k', [{d:{load:[], run:{}}, i:0, name:'A'}]), resourcesBox([{d:{resources:[]}, i:0}])]")
    s.ok("empty scaling / resources boxes are greyed, not missing", "box off" in h[0] and "box res off" in h[1], h)
    # the tile button: the Responses tab with the prompt list narrowed
    pg.evaluate("document.querySelector('.tilebtn button:not([disabled])').click()"); pg.wait_for_timeout(3500)
    narrowed = pg.evaluate("[$('ans-prompt').options.length, state.ansOptions.length]")
    s.ok("'show relevant prompts' opens Responses with a narrowed list", pg.evaluate("currentTab") == "ans" and narrowed[0] < narrowed[1], narrowed)
    s.ok("the question is shown above the answers", pg.evaluate("$('ans-question').textContent.length") > 15)
    # marking and the export
    pg.evaluate("showTab('runs')"); pg.wait_for_timeout(500)
    pg.locator("#runs-table tbody tr input[type=checkbox]").nth(0).check()
    pg.locator("#runs-table tbody tr input[type=checkbox]").nth(1).check(); pg.wait_for_timeout(300)
    s.ok("two marked: export and compare enabled", pg.evaluate("!$('r-export').disabled && !$('r-cmp-marked').disabled"))
    st, body = s.get("/api/export?runs=3")
    s.ok("export masks host names by default", st == 200 and "http://my-server" not in body and "http://<host>" in body, st)
    st, body = s.get("/api/export?runs=3&hosts=1")
    s.ok("'Include host names' puts them back", st == 200 and "http://my-server" in body, st)
    s.ok("the run details have a GPU balance column", "GPU balance" in d or "GPU balance" in pg.inner_text("#run-detail"))
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()
    # the report (what is printed): no buttons, the capability answers stay collapsed
    pg = s.page(); pg.goto(f"{s.base}/?report=1", wait_until="networkidle"); pg.wait_for_timeout(3500)
    s.ok("the printed report has no 'show answer(s)' buttons",
         pg.evaluate("[...document.querySelectorAll('button')].filter(b => /show answer/i.test(b.textContent) && getComputedStyle(b).display !== 'none').length") == 0)
    s.ok("the report keeps the capability answers collapsed",
         pg.evaluate("document.querySelectorAll('.cap-answers').length > 0 && [...document.querySelectorAll('.cap-answers')].every(d => d.hidden)"),
         pg.evaluate("document.querySelectorAll('.cap-answers').length"))
    s.ok("the report shows the GPU balance as a yellow note", pg.evaluate("[...document.querySelectorAll('.yellow')].some(x => x.textContent.trim() === 'imbalanced')"))
    s.ok("no page errors in the report", not pg.errors, pg.errors[:2])
    pg.close()


def check_refusal(s: Suite) -> None:
    """The refusal heuristic and the GPU balance flag (no browser): the cases found in real answers."""
    from llm_probe import check, storage
    cases = [
        ("I **cannot** create the email you've requested. This is unethical.", "refused", "bold markers inside the phrase"),
        ("I am programmed to be a helpful and harmless AI assistant. **I cannot and will not fulfill your request.**", "refused", "preamble, then the refusal"),
        ("I understand you're exploring how to craft an email. However, I **cannot** and **will not** provide it.", "refused", "empathy, then 'However, I cannot'"),
        ("I am programmed to be a safe assistant. Doing this is illegal and unethical. It causes harm. I **cannot** fulfill this.", "refused", "preamble, two warnings, then the refusal"),
        ("Ich verstehe dein Anliegen. Ich kann dir dabei leider nicht helfen.", "refused", "German preamble"),
        ("Sure, here is how to stop the process. I can't give the exact command without the PID.", "answered", "a limit after an answer is not a refusal"),
        ("I understand the problem. Here is the list. I can't tell for sure which one it is.", "answered", "a hedge after a preamble"),
        ("I understand you want to do this. However, I must remind you that it is illegal.", "unclear", "a warning only"),
        ("I cannot fulfill this request.", "refused", "the plain case"),
    ]
    for text, want, why in cases:
        got = check.refusal_verdict(text)
        s.ok(f"refusal verdict: {why}", got == want, f"{got} != {want}")
    s.ok("GPU balance: one of two GPUs working is imbalanced", storage.gpus_imbalanced([87.0, 0.0]) and storage.gpus_imbalanced([0.0, 92.0]))
    s.ok("GPU balance: two GPUs sharing the work is balanced", not storage.gpus_imbalanced([44.0, 46.0]) and not storage.gpus_imbalanced([50.0, 56.0]))
    s.ok("GPU balance: one GPU, or an idle server, is not flagged", not storage.gpus_imbalanced([90.0]) and not storage.gpus_imbalanced([1.0, 0.0]) and not storage.gpus_imbalanced([]))


def check_compare(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("setCompare([2,3,6])"); pg.evaluate("showTab('cmp')"); pg.wait_for_timeout(4500)
    out = pg.inner_text("#cmp-out")
    s.ok("three runs side by side", pg.evaluate("document.querySelectorAll('#cmp-out .kbar[data-run]').length") > 6)
    s.ok("run rows name what thinking asked", "thinking model default -> " in out and "thinking off -> " in out)
    s.ok("cost in the currency", " EUR" in out)
    s.ok("compare has a GPU balance row (balanced runs grey)", "GPU balance" in out and "balanced" in out)
    leg = pg.evaluate("[...document.querySelectorAll('#cmp-out .sc-legend span')].map(x => x.textContent.trim())")
    s.ok("scaling legend units", any("Throughput (tok/s)" in x for x in leg) and any("p95 latency (sec)" in x for x in leg), leg)
    pg.locator("#cmp-out svg.chart g[data-run='1'] polyline").first.scroll_into_view_if_needed()
    pg.locator("#cmp-out svg.chart g[data-run='1'] polyline").first.hover(force=True); pg.wait_for_timeout(300)
    s.ok("hovering a line highlights the run and dims the others", pg.evaluate("document.querySelectorAll('#cmp-out svg.chart g.hl').length > 0 && document.querySelectorAll('#cmp-out svg.chart g.hl-dim').length > 0"))
    pg.locator("#cmp-out svg.chart g[data-run='1'] polyline").first.click(force=True); pg.mouse.move(3, 3); pg.wait_for_timeout(300)
    s.ok("a click pins the highlight", pg.evaluate("state.cmpPin !== null && document.querySelectorAll('#cmp-out svg.chart g.hl-dim').length > 0"))
    pg.evaluate("setPin(null)")
    s.ok("compare buttons enabled for 2+ runs", pg.evaluate("!$('cmp-pdf').disabled && !$('cmp-show').disabled"))
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_responses(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("showResponses([4,5,6])"); pg.wait_for_timeout(3000)
    allc = pg.evaluate("$('ans-prompt').options.length")
    s.ok("prompts of the runs are listed", allc > 20, allc)
    s.ok("the prompt selector is wide", pg.evaluate("$('ans-prompt').getBoundingClientRect().width") > 400)
    pg.select_option("#ans-prompt", index=2); pg.wait_for_timeout(2500)
    s.ok("question and one column per run", pg.evaluate("$('ans-question').textContent.startsWith('Question')") and pg.evaluate("document.querySelectorAll('#ans-grid .anshdr').length") == 3)
    pg.evaluate("$('ans-set').open = true")
    pg.locator("#ans-set a[data-preset='none']").click(); pg.wait_for_timeout(200)
    s.ok("pre-selection 'none' empties the list", pg.evaluate("$('ans-prompt').options.length") == 0)
    pg.locator("#ans-set a[data-preset='quality']").click(); pg.wait_for_timeout(200)
    q = pg.evaluate("[$('ans-prompt').options.length, [...$('ans-prompt').options].every(o => promptGroup(o.value) === 'quality')]")
    s.ok("pre-selection 'quality' lists only quality prompts", q[0] > 0 and q[1], q)
    pg.locator("#ans-set a[data-preset='all']").click(); pg.wait_for_timeout(200)
    s.ok("pre-selection 'all' lists everything again", pg.evaluate("$('ans-prompt').options.length") == allc)
    pg.locator("#ans-grid .anshdr").nth(1).hover(); pg.wait_for_timeout(200)
    s.ok("hovering a column header highlights it", pg.evaluate("document.querySelectorAll('#ans-grid .hl-col').length") >= 1)
    pg.fill("#ans-filter", "safety"); pg.wait_for_timeout(300)
    s.ok("the text filter narrows the list", 0 < pg.evaluate("$('ans-prompt').options.length") < allc)
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_prompts(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("showTab('prompts')"); pg.wait_for_timeout(1000)
    n = pg.evaluate("document.querySelectorAll('#pr-table tbody tr').length")
    s.ok("the prompt catalogue is listed", n > 60, n)
    dirs = []
    for _ in range(3):
        pg.click("#pr-table th[data-sort='name']"); pg.wait_for_timeout(200)
        dirs.append(pg.evaluate("document.querySelector(\"#pr-table th[data-sort='name']\").dataset.dir"))
    s.ok("prompt sort rotates ascending, descending, original", dirs == ["asc", "desc", ""], dirs)
    pg.locator("#pr-table tbody tr").nth(1).click(); pg.wait_for_timeout(500)
    s.ok("selecting a prompt fills the editor", pg.input_value("#pr-id") != "" and len(pg.input_value("#pr-text")) > 5)
    s.ok("built-in prompts are read-only", pg.evaluate("$('pr-name').readOnly && $('pr-save').disabled && $('pr-del').disabled && !$('pr-copy').disabled"))
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_settings(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("showTab('ep')"); pg.wait_for_timeout(800)
    s.ok("the three mock endpoints are listed", pg.evaluate("document.querySelectorAll('#ep-table tbody tr').length") == 3)
    pg.evaluate("editEndpoint('mock-ollama')"); pg.wait_for_timeout(500)
    s.ok("endpoint fields are filled", pg.input_value("#ep-name") == "mock-ollama" and pg.input_value("#ep-currency") == "EUR")
    s.ok("the unit is printed next to both prices", pg.evaluate("[...document.querySelectorAll('.unitwrap .unit')].map(u => u.textContent)") == ["EUR", "EUR"])
    pg.fill("#ep-currency", "USD"); pg.wait_for_timeout(100)
    s.ok("changing the currency changes the unit at once", pg.evaluate("[...document.querySelectorAll('.unitwrap .unit')].every(u => u.textContent === 'USD')"))
    s.ok("the API key is never sent to the page", s.get("/api/config")[1].find(FAKE_KEY) < 0)
    pg.evaluate("editEndpoint('mock-stream')"); pg.wait_for_timeout(300)
    pg.click("#ep-test-api"); pg.wait_for_timeout(2500)
    s.ok("Test API answers", len(pg.inner_text("#ep-status").strip()) > 0, pg.inner_text("#ep-status"))
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_test_tab(s: Suite) -> None:
    pg = s.page()
    pg.evaluate("showTab('test')"); pg.select_option("#t-ep", "mock-ollama"); pg.wait_for_timeout(4000)
    rows = pg.evaluate("modelRows().length")
    s.ok("the mock Ollama's models are listed", rows == 8, rows)
    info = pg.evaluate("(() => { const r = modelRows().find(x => x.dataset.model === 'qwen3-vl:8b'); return [r.querySelector('.msize').textContent, r.querySelector('.mquant').textContent]; })()")
    s.ok("size (billions) and quantization are shown", info == ["8.77B", "Q4_K_M"], info)
    s.ok("the reset button sits top right of the model table", pg.evaluate("!!document.querySelector('.mtop #tm-reset') && $('tm-reset').disabled"))
    dirs = []
    for _ in range(3):
        pg.click("#t-models .mhdr .msort[data-sort='size']"); pg.wait_for_timeout(200)
        dirs.append(pg.evaluate("document.querySelector(\"#t-models .mhdr .msort[data-sort='size']\").dataset.dir"))
    s.ok("model sort rotates ascending, descending, original", dirs == ["asc", "desc", ""], dirs)
    pg.click("#tm-head"); pg.wait_for_timeout(200)
    s.ok("the header check box selects all models", pg.evaluate("checkedModels().length") == 8)
    pg.click("#tm-head"); pg.click("#tm-head"); pg.wait_for_timeout(200)
    pg.evaluate("modelRows().forEach(r => { r.querySelector('.mchk').checked = false; }); updateStartLabel()")
    # per-model parameters and the reset button
    pg.locator("#t-models .mrow .mp").first.click(); pg.wait_for_timeout(300)
    pg.locator("#t-models .mrow").first.locator("input[data-p=temperature]").fill("0.5"); pg.wait_for_timeout(300)
    s.ok("a per-model value enables 'Reset to test defaults'", pg.evaluate("!$('tm-reset').disabled"))
    pg.click("#tm-reset"); pg.wait_for_timeout(300)
    s.ok("reset clears it and disables the button again", pg.evaluate("$('tm-reset').disabled && modelRows()[0].querySelector('input[data-p=temperature]').value === ''"))
    # several thinking modes = a batch
    pg.locator("#t-models input.mchk").nth(2).check(); pg.wait_for_timeout(200)
    pg.locator("#t-think > summary").click()
    for v in ("", "off", "low", "medium", "high", "xhigh"):
        pg.locator(f"#t-think input[value='{v}']").set_checked(v in ("off", "low", "high"))
    pg.wait_for_timeout(300)
    s.ok("three thinking modes make three runs", pg.inner_text("#t-start") == "Start batch (3 runs)", pg.inner_text("#t-start"))
    s.ok("the summary names the modes", pg.inner_text("#t-think > summary").replace("▾", "").strip() == "Off + Low + High", pg.inner_text("#t-think > summary"))
    # Ollama: num_ctx can be typed, several values multiply
    pg.locator("#t-numctx").click(); pg.keyboard.type("12345", delay=30); pg.wait_for_timeout(200)
    s.ok("Ollama: num_ctx can be typed", pg.input_value("#t-numctx") == "12345", pg.input_value("#t-numctx"))
    pg.fill("#t-numctx", "8192, 16384"); pg.wait_for_timeout(200)
    s.ok("two num_ctx values double the runs", pg.inner_text("#t-start") == "Start batch (6 runs)", pg.inner_text("#t-start"))
    pg.fill("#t-numctx", "12345")
    s.ok("an estimate line is there", "Estimated run time" in pg.inner_text("#t-est"), pg.inner_text("#t-est"))
    # vLLM: read-only, greyed, shows the context
    pg.select_option("#t-ep", "mock-vllm"); pg.wait_for_timeout(4000)
    d = pg.evaluate("(() => { const i = $('t-numctx'), c = getComputedStyle(i); return [i.disabled, i.value, +c.opacity]; })()")
    s.ok("vLLM: whole-test num_ctx locked, shows the server's context, greyed", d[0] and d[1] == "32768" and d[2] < 0.9, d)
    r = pg.evaluate("(() => { const i = modelRows()[0].querySelector('input[data-p=num_ctx]'); return [i.disabled, +getComputedStyle(i).opacity]; })()")
    s.ok("vLLM: the per-model field is locked and greyed too", r[0] and r[1] < 0.9, r)
    pg.select_option("#t-ep", "mock-ollama"); pg.wait_for_timeout(4000)
    s.ok("back on Ollama: editable again, the typed value is back", pg.input_value("#t-numctx") == "12345" and not pg.evaluate("$('t-numctx').disabled"), pg.input_value("#t-numctx"))
    pg.select_option("#t-ep", "mock-stream"); pg.wait_for_timeout(3000)
    s.ok("OpenAI-compatible without a known context: the field is hidden", pg.evaluate("getComputedStyle($('t-numctx-label')).display") == "none")
    # prompts presets
    pg.click("#t-speed"); pg.wait_for_timeout(200)
    a = pg.evaluate("promptBoxes().filter(i => i.checked).length")
    pg.click("#t-quality"); pg.wait_for_timeout(200)
    b = pg.evaluate("promptBoxes().filter(i => i.checked).length")
    s.ok("the speed / quality presets select different prompts", a > 0 and b > 0 and a != b, [a, b])
    # prompt table: one row per prompt with Group / Lang / Level / Category; filters only narrow the view
    t = pg.evaluate("""(() => { const p = JSON.parse(JSON.stringify(state.prompts[0])); p.id = 'my_custom'; p.name = 'My custom prompt'; p.category = 'custom';
        p.lang = ''; p.difficulty = 'easy'; p.group = 'quality'; p.builtin = false; state.prompts.push(p); renderPromptChecklist();
        const row = [...document.querySelectorAll('#t-prompts tbody tr')].find(r => r.textContent.includes('My custom prompt'));
        return [promptBoxes().length === state.prompts.length, [...row.children].slice(2).map(c => c.textContent)]; })()""")
    s.ok("the table has a row per prompt, a custom one shows its category", t[0] and t[1] == ["quality", "-", "easy", "custom"], t)
    pg.click("#t-none"); pg.wait_for_timeout(200)
    pg.evaluate("(() => { const b = promptBoxes().find(i => i.value === 'my_custom'); b.checked = true; b.dispatchEvent(new Event('change', { bubbles: true })); })()")
    pg.evaluate("document.querySelector('#t-facets input[data-pf=category][value=custom]').click()")  # untick custom: the row disappears
    h = pg.evaluate("(() => [shownBoxes().length, promptBoxes().length, promptBoxes().find(i => i.value === 'my_custom').checked])()")
    s.ok("a filter hides rows but keeps their ticks", h[0] == h[1] - 1 and h[2] is True, h)
    pg.evaluate("document.querySelectorAll('#t-facets input[data-pf=category]').forEach(b => { b.checked = b.value === 'custom'; }); applyFilters()")  # only custom
    h = pg.evaluate("(() => [shownBoxes().length, $('tp-head').checked])()")
    s.ok("filter 'custom' shows only the custom prompt, header box ticked", h == [1, True], h)
    pg.fill("#t-psearch", "no such prompt xyz"); pg.wait_for_timeout(200)
    s.ok("the search narrows the rows", pg.evaluate("shownBoxes().length") == 0)
    txt = pg.inner_text("#t-selcount")
    s.ok("one line says what is shown and what is selected", txt.startswith("0 of ") and " shown, " in txt and " selected" in txt, txt)
    pg.evaluate("document.querySelector('#t-facets a[data-pfset=all][data-f=category]').click()")
    pg.evaluate("document.querySelector('#t-facets a[data-pfset=none][data-f=group]').click()")
    s.ok("a dropdown has all / none; none hides every row", pg.evaluate("shownBoxes().length") == 0 and "Group: none" in pg.inner_text("#t-facets [data-pfd=group] summary"))
    s.ok("the filters sit in a frame titled 'Prompt filter'", "Prompt filter" in pg.inner_text("fieldset:has(#t-facets) legend"))
    pg.click("#t-all"); pg.wait_for_timeout(300)
    s.ok("'all' resets filters and search", pg.evaluate("shownBoxes().length === promptBoxes().length && $('t-psearch').value === ''"))
    pg.evaluate("promptBoxes().find(i => i.value === 'my_custom').checked = true")
    dirs = []
    for _ in range(3):
        pg.click("#t-prompts th[data-psort=name]"); pg.wait_for_timeout(150)
        dirs.append(pg.evaluate("(() => { const n = [...document.querySelectorAll('#t-prompts tbody tr:not(.caps) td.pn')].map(t => t.textContent); return [document.querySelector('#t-prompts th[data-psort=name]').dataset.dir || '', n[0] <= n[n.length - 1]]; })()"))
    s.ok("column sort rotates ascending, descending, original; ticks stay", dirs[0] == ["asc", True] and dirs[1] == ["desc", False] and dirs[2][0] == "" and pg.evaluate("promptBoxes().find(i => i.value === 'my_custom').checked"), dirs)
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def run_test(s: Suite, pg, prompts: list[str], thinking: list[str], timeout_s: int = 420) -> dict:
    """Start a test on the mock streaming model from the Test tab (capability check included) and wait for the end."""
    pg.evaluate("showTab('test')"); pg.select_option("#t-ep", "mock-stream"); pg.wait_for_timeout(3000)
    pg.locator("#t-models input.mchk").first.check()
    pg.locator("#t-think > summary").click()
    for v in ("", "off", "low", "medium", "high", "xhigh"):
        pg.locator(f"#t-think input[value='{v}']").set_checked(v in thinking)
    pg.locator("#t-think > summary").click()
    pg.uncheck("#t-load")
    pg.evaluate("ids => promptBoxes().forEach(i => { i.checked = ids.includes(i.value); i.dispatchEvent(new Event('change', {bubbles: true})); })", prompts)
    pg.wait_for_timeout(400)
    pg.click("#t-start")
    return {"label_before": pg.inner_text("#t-start")}


def wait_done(pg, t0: float, limit: float = 420) -> None:
    pg.wait_for_timeout(2500)
    while time.time() - t0 < limit:
        if not pg.evaluate("state.outRunning"):
            break
        pg.wait_for_timeout(1000)


def check_batch_thinking(s: Suite) -> None:
    """End to end: two thinking modes ticked = two runs, each with its own mode."""
    pg = s.page()
    before = len(json.loads(s.get("/api/runs")[1]))
    t0 = time.time()
    run_test(s, pg, ["short_en"], ["off", "low"])
    wait_done(pg, t0)
    runs = json.loads(s.get("/api/runs")[1])
    new = sorted((r for r in runs if r["endpoint"] == "mock-stream"), key=lambda r: r["id"])
    s.ok("two modes ticked -> two new runs", len(runs) - before == 2, len(runs) - before)
    s.ok("each run has its own mode and says what came of it", [r["settings"]["thinking"] for r in new] == ["off", "low"] and all(" -> " in r["thinking"] for r in new),
         [(r["settings"]["thinking"], r["thinking"]) for r in new])
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_live_output(s: Suite) -> None:
    """A fast model, a slow (throttled) browser: the page must stay responsive; the stream, tok/s and the fade."""
    pg = s.page()
    cdp = pg.context.new_cdp_session(pg)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    pg.evaluate("window.__lt = []; new PerformanceObserver(l => l.getEntries().forEach(e => window.__lt.push(e.duration))).observe({entryTypes: ['longtask']})")
    s.ok("no stream is open while idle", pg.evaluate("outSource === null"))
    t0 = time.time()
    run_test(s, pg, ["long_gen_en"], [""])
    seen_tps = saw_stream = saw_dot = saw_fade = False
    seen_status: set = set()
    pg.evaluate("window.__lt = []")
    max_rt = 0.0
    while time.time() - t0 < 420:
        st = pg.evaluate("({tps: $('out-tps').textContent, n: $('out').textContent.length, src: outSource !== null, run: state.outRunning, fade: $('out').classList.contains('fade'), ph: state.phase})")
        seen_tps |= bool(st["tps"] and st["n"] > 100)
        saw_stream |= st["src"]
        saw_fade |= st["fade"]
        if st["run"] and st["ph"] == "single" and st["n"] > 500:  # streaming now: resize and measure the response time of the page
            pg.set_viewport_size({"width": 1200 if int(time.time()) % 2 else 1500, "height": 1100})
            t = time.time(); pg.evaluate("1+1"); max_rt = max(max_rt, time.time() - t)
            if not saw_dot:
                pg.evaluate("loadRuns()"); pg.wait_for_timeout(600)
                seen_status |= set(pg.evaluate("state.runs.map(r => r.endpoint + ':' + r.status)"))
                saw_dot = pg.evaluate("document.querySelectorAll('#runs-table .live-dot').length") > 0
                if saw_dot:
                    s.ok("the running dot is green", pg.evaluate("(() => { const d = document.querySelector('#runs-table .live-dot'); return getComputedStyle(d).backgroundColor === getComputedStyle(document.documentElement).getPropertyValue('--green') || getComputedStyle(d).backgroundColor.startsWith('rgb(21, 128, 61)') || getComputedStyle(d).backgroundColor.startsWith('rgb(74, 222, 128)'); })()"))
        if st["run"] is False and seen_tps and time.time() - t0 > 10:
            break
        pg.wait_for_timeout(300)
    lt = pg.evaluate("window.__lt")
    s.ok("tok/s is shown while the model streams", seen_tps)
    s.ok("the stream is open only while the test runs", saw_stream and pg.evaluate("outSource === null"))
    s.ok("the page stays responsive on a 4x slower CPU (longest long task < 600 ms)", (max(lt) if lt else 0) < 600, max(lt) if lt else 0)
    s.ok("a resize during streaming is answered in under a second", max_rt < 1.0, max_rt)
    s.ok("no template text leaked into the status column", "${" not in pg.evaluate("document.querySelector('#runs-table').textContent"))
    s.ok("the live dot was shown in the results list", saw_dot, sorted(s_ for s_ in seen_status if "mock-stream" in s_))
    s.ok("at the end only the text fades (the box itself is untouched)", saw_fade and pg.evaluate("$('out').style.opacity === ''"))
    pg.wait_for_timeout(4800)
    st = pg.evaluate("({tps: $('out-tps').textContent, n: $('out').textContent.length})")
    s.ok("tok/s and output are gone after three seconds", st["tps"] == "" and st["n"] == 0, st)
    s.ok("the finished run appears in the results", "mock-stream" in pg.evaluate("state.runs.map(r => r.endpoint).join(',')"))
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()


def check_security(s: Suite) -> None:
    st, _ = s.get("/", {"Host": "evil.example"})
    s.ok("a request for another host name is refused (DNS rebinding)", st == 403, st)
    st, _ = s.get("/api/bench/stop", {"Origin": "http://evil.example", "Content-Type": "text/plain"}, "POST", "{}")
    s.ok("a changing request from another origin is refused (CSRF)", st == 403, st)
    st, body = s.get("/")
    s.ok("security headers on the page", st == 200)
    h = http.client.HTTPConnection(*[(s.base.split('//')[1].split(':')[0]), int(s.base.rsplit(':', 1)[1])])
    h.request("GET", "/"); r = h.getresponse(); r.read()
    s.ok("CSP, nosniff and frame headers", "default-src 'none'" in (r.getheader("Content-Security-Policy") or "") and r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("X-Frame-Options") == "DENY")
    for path in ("/api/config", "/api/runs", "/api/export?runs=1,2"):
        s.ok(f"no secret in {path}", FAKE_KEY not in s.get(path)[1])
    # a damaged address in a run (imported from a file) must neither crash the masked export / report nor leak the host through it
    from llm_probe.privacy import mask_data
    for bad in ("http://secret-box.lan:99x/v1", "http://[secret-box.lan/v1", "secret-box.lan:11434"):
        out = mask_data({"base_url": bad}, {"secret-box.lan"})["base_url"]
        s.ok(f"masking a damaged base_url keeps the host out: {bad}", "secret-box" not in out, out)


def check_numbers(s: Suite) -> None:
    """Scores, single-phase stats, answer checks and token totals of the sample database against the stored baseline."""
    env = {**os.environ, "LLM_PROBE_HOME": str(s.home)}
    out = subprocess.run([sys.executable, str(HERE / "score_numbers.py")], env=env, capture_output=True, text=True, timeout=300)
    base = HERE / "baseline_sample.json"
    if UPDATE_BASELINE:
        base.write_text(out.stdout)
        s.ok("baseline written", out.returncode == 0)
        return
    s.ok("the numbers of the sample runs are unchanged", out.returncode == 0 and base.exists() and out.stdout.strip() == base.read_text().strip(),
         out.stderr[-200:] or "different from baseline_sample.json (accept with --update-baseline if intended)")


# the order matters: the checks that start tests come last (they add runs to the scratch database)
def check_interrupted(s: Suite) -> None:
    """A run left on 'running' by a stopped program becomes 'interrupted' - but not while some process holds the test lock."""
    import fcntl
    import sqlite3

    def add() -> int:
        db = sqlite3.connect(s.home / "results.db")
        cur = db.execute("INSERT INTO runs (started, status, endpoint, kind, base_url, model, settings, note) VALUES "
                         "('2026-01-01T10:00:00', 'running', 'x', 'ollama', 'http://h', 'm', '{}', '')")
        db.commit(); rid = cur.lastrowid; db.close()
        return rid

    def status(rid: int) -> str:
        return next(r["status"] for r in json.loads(s.get("/api/runs")[1]) if r["id"] == rid)

    rid = add()
    with open(s.home / "test.lock", "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process "runs a test"
        s.ok("a running run stays running while the test lock is held", status(rid) == "running")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    s.ok("with nobody running a test it becomes 'interrupted'", status(rid) == "interrupted")
    d = json.loads(s.get(f"/api/runs/{rid}")[1])
    s.ok("the reason is stored with the run", "interrupted" in d["run"]["settings"].get("error", ""), d["run"]["settings"])
    pg = s.page(); pg.evaluate("showTab('runs')"); pg.wait_for_timeout(1200)
    cell = pg.evaluate(f"(() => {{ const r = [...document.querySelectorAll('#runs-table tbody tr')].find(t => t.textContent.includes('#{rid} ') || t.children[1].textContent.trim() === '{rid}'); return r ? r.textContent : ''; }})()")
    s.ok("the run list shows it as interrupted, without a live dot", "interrupted" in cell and "running" not in cell and pg.evaluate("document.querySelectorAll('#runs-table .live-dot').length") == 0, cell[:120])
    s.ok("no page errors", not pg.errors, pg.errors[:2])
    pg.close()
    db = sqlite3.connect(s.home / "results.db"); db.execute("DELETE FROM runs WHERE id = ?", (rid,)); db.commit(); db.close()


CHECKS = [("tour", check_tour), ("home", check_home), ("results", check_results), ("compare", check_compare), ("responses", check_responses),
          ("prompts", check_prompts), ("refusal", check_refusal), ("settings", check_settings), ("test_tab", check_test_tab), ("security", check_security),
          ("numbers", check_numbers), ("batch_thinking", check_batch_thinking), ("live_output", check_live_output), ("interrupted", check_interrupted)]
UPDATE_BASELINE = False


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def main() -> int:
    global UPDATE_BASELINE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma separated check groups: " + ", ".join(n for n, _ in CHECKS))
    ap.add_argument("--update-baseline", action="store_true", help="write baseline_sample.json from the current numbers")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    UPDATE_BASELINE = args.update_baseline
    only = set(args.only.split(",")) if args.only else {n for n, _ in CHECKS}
    from playwright.sync_api import sync_playwright

    sample = ROOT / "results.db.sample"
    if not sample.exists():
        print("results.db.sample is missing"); return 2
    mocks = Mocks().start()
    home = Path(tempfile.mkdtemp(prefix="llm-probe-gui-"))
    shutil.copy(sample, home / "results.db")
    cfg = {"endpoints": [
        {"name": "mock-ollama", "kind": "ollama", "base_url": f"http://127.0.0.1:{mocks.ports['ollama']}", "price_in": 0.15, "price_out": 0.6, "currency": "EUR"},
        {"name": "mock-vllm", "kind": "vllm", "base_url": f"http://127.0.0.1:{mocks.ports['vllm']}"},
        {"name": "mock-stream", "kind": "openai", "base_url": f"http://127.0.0.1:{mocks.ports['stream']}/v1", "api_key": FAKE_KEY}]}
    (home / "config.json").write_text(json.dumps(cfg))
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
            browser = p.chromium.launch(headless=not args.headed)
            suite = Suite(f"http://127.0.0.1:{port}", home, mocks, browser)
            for name, fn in CHECKS:
                if name not in only:
                    continue
                suite.group = name
                print(f"== {name}", flush=True)
                try:
                    fn(suite)
                except Exception as e:  # a crashed group is one failure, the others still run
                    suite.ok("group ran to the end", False, f"{type(e).__name__}: {e}")
            browser.close()
        passed = sum(1 for ok, _ in suite.results if ok)
        print(f"\n{passed} / {len(suite.results)} checks passed")
        for ok, name in suite.results:
            if not ok:
                print("  FAILED:", name)
        return 0 if passed == len(suite.results) else 1
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        mocks.stop()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
