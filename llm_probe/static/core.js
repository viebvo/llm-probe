"use strict";
// helpers, resizable columns, shared list behaviour - part of the page, loaded in the order of the <script> tags in index.html
const $ = (id) => document.getElementById(id);
const LETTERS = "ABCDEFGHIJKLMNOP";
const state = { promptImages: [], marked: new Set(), cfg: null, prompts: [], runs: [], selectedRun: null, epOriginal: null, benchSeq: 0,
                benchRunning: false, answers: null, ansRuns: [], out: { id: -1, think: 0, text: 0 } };

// ------------------------------------------------------------------ helpers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(v, digits = 1, unit = "") { return v == null ? "-" : v.toFixed(digits) + unit; }
// whole-run wall-clock time from the stored ISO timestamps (local time, "...T..:..:..", so a plain Date diff works)
function runDuration(run) {
  if (!run || !run.started || !run.finished) return null;
  const s = Date.parse(run.started), f = Date.parse(run.finished);
  return isNaN(s) || isNaN(f) || f < s ? null : (f - s) / 1000;  // seconds
}
function fmtDuration(sec) {
  if (sec == null) return "-";
  const s = Math.round(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} m` : m ? `${m} m ${String(r).padStart(2, "0")} s` : `${r} s`;
}
// how the overview shows a run's total runtime: a finished run's duration, "running ..." while in progress, else "-"
function runtimeLabel(run) {
  return run && run.status === "running" ? "running ..." : fmtDuration(runDuration(run));
}
// a prompt the editor cannot build or reproduce: a long coding session (its steps come from the code, not the form).
// tools/turns/provenance stay editable via "save under a new ID" (the server keeps them), so they are not locked.
function isCodeBased(p) { return !!(p.session && Object.keys(p.session).length); }
function delta(a, b, higherBetter = true) {
  if (a == null || b == null || a === 0) return '<span class="dim">-</span>';
  const d = (b - a) / a * 100;
  const better = higherBetter ? d > 0 : d < 0;
  const cls = Math.abs(d) < 3 ? "dim" : better ? "green" : "red";
  const verdict = Math.abs(d) < 3 ? "practically equal" : better ? "B is better" : "B is worse";
  const tip = `B ${d >= 0 ? "+" : ""}${d.toFixed(1)} % relative to A (${higherBetter ? "higher" : "lower"} is better): ${verdict}`;
  return `<span class="${cls}" title="${tip}">${d >= 0 ? "+" : ""}${d.toFixed(0)} %</span>`;
}
function avg(values) {
  const v = values.filter((x) => x != null);
  return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
}
// ---------------------------------------------------------------- resizable table columns
// Every table gets a drag handle at the right edge of each header cell. The widths are remembered in this browser
// per table (recognized by its headers); a double click on a handle resets the table to automatic widths.
function tableKey(t) {
  return "colw:" + (t.id || [...t.querySelectorAll("thead tr:first-child > th")].map((th) => th.textContent.trim()).join("|"));
}
function applyWidths(t, widths) {
  const ths = [...t.querySelectorAll("thead tr:first-child > th")];
  const ok = widths && widths.length === ths.length;
  ths.forEach((th, i) => { th.style.width = ok ? widths[i] + "px" : ""; });
  t.style.width = ok ? widths.reduce((a, b) => a + b, 0) + "px" : "";
  t.classList.toggle("sized", !!ok);
}
function initResize(t) {
  t.dataset.rz = "1";
  const ths = [...t.querySelectorAll("thead tr:first-child > th")];
  if (!ths.length) return;
  try { applyWidths(t, JSON.parse(recall(tableKey(t)) || "null")); } catch (e) { /* no stored widths */ }
  ths.forEach((th, i) => {
    const h = document.createElement("span");
    h.className = "rz";
    h.title = "Drag to change the column width; double click: automatic widths for the whole table";
    th.append(h);
    h.addEventListener("click", (ev) => ev.stopPropagation());  // not a click on the header (sorting)
    h.addEventListener("dblclick", (ev) => {
      ev.stopPropagation();
      try { localStorage.removeItem(tableKey(t)); } catch (e) { /* storage unavailable */ }
      applyWidths(t, null);
    });
    h.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const widths = ths.map((x) => x.getBoundingClientRect().width);  // fix all columns at their current width
      const x0 = ev.clientX, w0 = widths[i];
      applyWidths(t, widths);
      h.setPointerCapture(ev.pointerId);
      h.classList.add("active");
      const move = (e) => { widths[i] = Math.max(28, w0 + e.clientX - x0); applyWidths(t, widths); };
      const up = () => {
        h.removeEventListener("pointermove", move);
        h.removeEventListener("pointerup", up);
        h.classList.remove("active");
        store(tableKey(t), JSON.stringify(widths.map(Math.round)));
      };
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", up);
    });
  });
}
// dragging a panel's resize grip lifts its max-height, so it can also be made taller than the default
document.addEventListener("pointerdown", (ev) => {
  const el = ev.target;
  if (!(el instanceof HTMLElement) || getComputedStyle(el).resize === "none") return;
  const r = el.getBoundingClientRect();
  if (ev.clientX > r.right - 22 && ev.clientY > r.bottom - 22) el.style.maxHeight = "none";
}, true);
// a cell cut short by a narrowed column shows its full text on hover
document.addEventListener("mouseover", (ev) => {
  const td = ev.target.closest && ev.target.closest("table.sized td");
  if (td && !td.title && td.scrollWidth > td.clientWidth) td.title = td.textContent.trim();
});
// tables are rendered again and again: give every new one its handles (once per animation frame)
let resizeQueued = false;
new MutationObserver(() => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => {
    resizeQueued = false;
    document.querySelectorAll("table:not([data-rz])").forEach(initResize);
  });
}).observe(document.body, { childList: true, subtree: true });

function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ } }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
// per browser tab: a newly opened page starts on Home, a reload keeps the page
// "Include host names" (Results and Compare share it): off = exports and printed reports hide the server addresses
function hostsParam() { return $("r-hosts").checked ? "&hosts=1" : ""; }
document.addEventListener("change", (ev) => {
  if (ev.target.id === "r-hosts" || ev.target.id === "cmp-hosts") $("r-hosts").checked = $("cmp-hosts").checked = ev.target.checked;
});
// ---- shared list behaviour (one way to operate every list and table, see the design rules in the README)
// Header check box: every click goes on in the order all -> inverted -> none -> your own selection -> all ... A stage that
// would change nothing is skipped; ticking a box by hand starts a new round (your selection is then the new "own selection").
// boxes() = the row check boxes (disabled ones are left alone); onChange() runs after every change.
function bindHeadCheck(head, boxes, onChange) {
  let snap = null, last = null, stage = 0;
  const keyOf = (b) => b.value;
  const ticked = () => new Set(boxes().filter((b) => !b.disabled && b.checked).map(keyOf));
  const same = (a, b) => a.size === b.size && [...a].every((k) => b.has(k));
  head.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const rows = boxes().filter((b) => !b.disabled), now = ticked();
    if (!last || !same(now, last)) { snap = now; stage = 0; }
    const everything = new Set(rows.map(keyOf));
    const sets = [everything, new Set([...everything].filter((k) => !snap.has(k))), new Set(), snap];
    let next = now;
    for (let i = 0; i < 4 && same(next, now); i++) { next = sets[stage]; stage = (stage + 1) % 4; }
    rows.forEach((b) => { b.checked = next.has(keyOf(b)); });
    last = next;
    syncHeadCheck(head, boxes);
    onChange();
  });
}
// the header check box shows all / some / none of the (enabled) row boxes
function syncHeadCheck(head, boxes) {
  const rows = boxes().filter((b) => !b.disabled), n = rows.filter((b) => b.checked).length;
  head.checked = rows.length > 0 && n === rows.length;
  head.indeterminate = n > 0 && n < rows.length;
  head.disabled = !rows.length;
}
// Sortable column header: ascending -> descending -> not sorted (the table's own order). cur = { key, desc } or null.
function nextSort(cur, key) {
  if (!cur || cur.key !== key) return { key, desc: false };
  return cur.desc ? { key: null, desc: false } : { key, desc: true };
}
const SORT_TIP = "Click to sort ascending, again for descending, again for the original order.";

function storeTab(name) { try { sessionStorage.setItem("tab", name); } catch (e) { /* storage unavailable */ } }
function recallTab() { try { return sessionStorage.getItem("tab"); } catch (e) { return null; } }

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  if (state.report && path.startsWith("/api/")) path += (path.includes("?") ? "&" : "?") + "mask=1" + (state.reportHosts ? "&hosts=1" : "");  // a printed report shows no server addresses
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${r.status}`), { status: r.status, data });
  return data;
}
// "Wait ..." hint: a small pill in the middle of the page while something loads or renders. The class is set before
// the work and one frame is yielded, so the CSS fade-in (a compositor animation, it keeps running while the page
// renders) starts even if the work then blocks the page; it only shows if the work takes longer than ~0.25 s.
let busyCount = 0;
const nextFrame = () => new Promise((res) => { requestAnimationFrame(() => setTimeout(res, 0)); setTimeout(res, 60); });  // 2nd: hidden tab
function busyOn() { if (busyCount++ === 0) document.body.classList.add("busy"); }
function busyOff() { if (--busyCount <= 0) { busyCount = 0; document.body.classList.remove("busy"); } }
async function busy(fn) {
  busyOn();
  await nextFrame();
  try { return await fn(); } finally { busyOff(); }
}
function setMsg(el, text, cls) { el.className = "msg " + (cls || ""); el.textContent = text; }
function fillSelect(sel, options, placeholder, keep = true) {
  const current = sel.value;
  sel.innerHTML = (placeholder ? `<option value="">${esc(placeholder)}</option>` : "") +
    options.map(([label, value]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  if (keep && options.some(([, v]) => String(v) === current)) sel.value = current;
}
// the text of a cell without its markup, for comparing cells (an inert template: nothing in it runs or loads)
const plainEl = document.createElement("template");
function plainText(html) { plainEl.innerHTML = html; return plainEl.content.textContent; }
// Group shading: rows that share the first-column value (a group) get the same tint; it flips when the value
// changes. A blank first cell is a continuation of the group above; a group-heading row (cls "grp") resets it.
function shadeRows(rows) {
  let shade = 0, prevKey = null;
  return rows.map((r) => {
    if (r.cls && /grp/.test(r.cls)) { prevKey = null; return r; }
    const cells = r.cells || r;
    let key = r.group != null ? String(r.group) : plainText(String(cells[0] ?? "")).trim();
    if (key === "") key = prevKey;  // continuation row (e.g. further repetitions of the same prompt)
    if (prevKey === null) shade = 0;
    else if (key !== prevKey) shade ^= 1;
    prevKey = key;
    return { ...(r.cells ? r : { cells: r }), cls: `${r.cls || ""} sh${shade}`.trim() };
  });
}
// sort = { id, keys (a sort key per header, null = not sortable), cur ({ key, desc } or undefined) }: clickable headers
function table(caption, headers, rows, sort) {
  const th = (h, i) => {
    const key = sort && sort.keys[i], on = key && sort.cur && sort.cur.key === key;
    return `<th${key ? ` class="sort" data-tbl="${sort.id}" data-sort="${key}"` : ""}${on ? ` data-dir="${sort.cur.desc ? "desc" : "asc"}"` : ""}` +
      `${tipAttr([colTip(h), key ? SORT_TIP : ""].filter(Boolean).join("\n\n"))}>${esc(h)}</th>`;
  };
  return `<div class="table-wrap"><table>${caption ? `<caption>${esc(caption)}</caption>` : ""}<thead><tr>` +
    headers.map(th).join("") + "</tr></thead><tbody>" +
    shadeRows(rows).map((r) => `<tr${r.cls ? ` class="${r.cls}"` : ""}>` +
      (r.cells || r).map((c) => `<td${r.span ? ` colspan="${r.span}"` : ""}>${c}</td>`).join("") + "</tr>").join("") +
    "</tbody></table></div>";
}
// responses of a prompt that stopped at a limit, e.g. "2 tok" (max_tokens) / "1 time" (time limit)
function cutLabel(s) {
  const parts = [s.cut_tokens ? `${s.cut_tokens} tok` : "", s.cut_time ? `${s.cut_time} time` : ""].filter(Boolean);
  return parts.length ? `<span class="yellow">${parts.join(", ")}</span>` : "";
}
const CUT_TEXT = { tokens: "cut off at max_tokens", time: "cut off at the time limit",
                   context: "prompt cut by the server - the context per request is too small; not counted" };
// truncation marker: a purple "!" (distinct from error red), the response is still graded on what came back
function cutMark(cut) {
  return cut ? `<span class="cut-mark" title="${esc(CUT_TEXT[cut] + " – still graded on what came back")}">!</span> ` +
    `<span class="cut-mark">${CUT_TEXT[cut]}</span>` : "";
}
function checkLabel(s) {
  return s && s.check_n ? fmt(s.check_score * 100, 0, " %") : "";
}
// one square per checked line / value: green with its number = correct, red x = wrong (hover: expected and got);
// a "knowledge vs. reading" probe item is inverted - a green check = the model did NOT leak the unreadable answer
function checkSquares(c) {
  return '<div class="squares">' + c.items.map((i, n) => {
    const got = i.got == null ? "missing" : i.got;
    const cls = c.undecided ? "unk" : i.ok ? "ok" : "bad";
    const title = i.provenance
      ? `${i.label || n + 1} (read vs. recall probe)\nnot readable from the image; pass = did NOT answer "${i.expected.replace(/^\d+:\s*/, "")}"\ngot: ${got}\n${i.ok ? "pass - did not leak it" : "leak - produced an answer it could not have read"}`
      : `${i.label || n + 1}\nexpected: ${i.expected}\ngot: ${got}`;
    const ch = c.undecided ? "?" : i.unsolvable ? "∅" : i.provenance ? (i.ok ? "✓" : "!") : (i.ok ? n + 1 : "x");
    const note = i.unsolvable ? `\nthe model answered "unsolvable" (escape route): it said it could not answer` : "";
    return `<span class="sq ${cls}" title="${esc(title + note)}">${ch}</span>`;
  }).join("") + "</div>";
}

// answer checks of a run: every successful response of the single phase, one row per repetition
function checksCard(d) {
  const pids = Object.keys(d.checks || {});
  if (!pids.length) return "";
  const rows = pids.flatMap((pid) => d.checks[pid].map((c, k) => ({ cls: k ? "" : "first", cells: [
    k ? "" : esc(promptName(pid)) + ctxNote([{ L: "", d }], pid), c.rep + 1, checkSquares(c), `<span class="cap-detail">${checkVerdict(c)}</span>`,
    k ? "" : `<button class="btn small no-print" title="Open this response in the Responses tab" ` +
      `onclick="showAnswerFor(${d.run.id}, &quot;${esc(pid)}&quot;)">show answer(s)</button>`] })));
  return box("checks", "Answer checks", '<p class="hint">Every response with an expected answer, ' +
    "one row per repetition: a green square per correct line or value, a red x per wrong one, a ? where the check " +
    "cannot decide (refusal tests: unclear, not counted; hover for expected and received). Reasoning is removed " +
    'before checking. "Show answer(s)" opens the response in the Responses tab.</p>' +
    table("", ["Prompt", "Rep.", "Checked lines", "Check result", ""], rows), { cls: "checks-card" });
}
// jump from an Answer-checks row to the Responses tab, showing that run's responses for that prompt
async function showAnswerFor(runId, pid) {
  state.ansSet = null;
  state.ansRuns = [runId];
  store("ans-runs", String(runId));
  renderAnsPicker();
  showTab("ans", true);
  await renderResponses();  // loads the run's prompt status and fills the dropdown
  const sel = $("ans-prompt");
  if ([...sel.options].some((o) => o.value === pid)) { sel.value = pid; await renderAnswers(); }
}

function checkVerdict(c) {
  if (!c.answered) {
    return `<span class="red">0/${c.total} no answer</span> ` + (c.cut ? `(${CUT_TEXT[c.cut]} in the reasoning - ` +
      `raise ${c.cut === "time" ? "the timeout" : "max_tokens"})` : "(only reasoning - cut off? raise max_tokens)");
  }
  const cutNote = c.cut ? ` <span class="yellow">(${CUT_TEXT[c.cut]})</span>` : "";
  if (c.cut === "context") return `<span class="yellow">${CUT_TEXT.context}</span>`;
  if (c.undecided) return `<span class="yellow">${esc(c.items[0].got)}</span>${cutNote}`;
  const cls = c.correct === c.total ? "green" : "red";  // correctness only
  let wrong = c.items.filter((i) => !i.ok).map((i) => {
    // "7: 65" lines: show the value only
    const labeled = i.expected.includes(":") && i.label === i.expected.split(":")[0].trim();
    let got = i.got == null ? null : labeled ? i.got.slice(i.got.indexOf(":") + 1).trim() : i.got;
    if (got && got.length > 40) got = got.slice(0, 37) + "...";
    return `${i.label || i.expected} (${got == null ? "missing" : `got '${got}'`})`;
  });
  if (wrong.length > 6) wrong = [...wrong.slice(0, 6), `+${wrong.length - 6} more`];
  const head = c.cer != null ? `CER ${fmt(c.cer * 100, 1, " %")} (${c.correct}/${c.total} lines)` : `${c.correct}/${c.total}`;
  const escape = c.unsolvable ? ` <span class="dim" title="The model answered &quot;unsolvable&quot; (the escape route offered by the hard prompts) instead of guessing; this does not change the score.">· ${c.unsolvable} unsolvable</span>` : "";
  return `<span class="${cls}">${head}</span>${escape}${cutNote}` +
    (wrong.length ? ` &nbsp;wrong: ${esc(wrong.join(", "))}` : "");
}
// state.prompts = the prompts offered for tests
const setPrompts = (all) => { state.prompts = all; };
const isAdaptive = (id) => !!(lookupPrompt(id) || {}).adaptive;
// token counts in k = 1024 (a context of 32768 reads "32k", 6912 "6.8k")
const fmtK = (n) => (n == null ? "?" : n >= 1024 ? (n / 1024).toFixed(1).replace(/\.0$/, "") + "k" : String(n));
// the context a prompt generated at run time (log triage sized to the context) was made for, next to its title; in a
// comparison it turns yellow when the runs were not tested with the same context
function ctxNote(runs, pid) {
  const info = runs.map((r) => ((r.d.run.settings || {}).adaptive || {})[pid]);
  if (!info.some(Boolean)) return "";
  const txt = (a) => (a ? `ctx ${fmtK(a.context)} · ~${fmtK(a.est_tokens)} tok` : "not run");
  const same = new Set(info.map((a) => (a ? a.context : null))).size === 1;
  const body = runs.length > 1 && !same ? info.map((a, n) => `${runs[n].L} ${txt(a)}`).join(" / ") : txt(info.find(Boolean));
  const tip = runs.length > 1 && !same ? "The runs were tested with different context sizes, so this prompt (generated to fit the context) differs between them."
    : "The prompt is generated when a run starts: its size follows the context the model is served with (effective context per request, tokens of the prompt).";
  return ` <span class="ctxnote ${runs.length > 1 && !same ? "yellow" : "dim"}" title="${esc(tip)}">${esc(body)}</span>`;
}
const lookupPrompt = (id) => state.prompts.find((p) => p.id === id);
function promptName(id) { return (lookupPrompt(id) || {}).name || id; }
// group of a prompt (Prompt.group); a prompt deleted since counts as performance - it cannot be checked any more
function promptGroup(id) { return (lookupPrompt(id) || {}).group || "performance"; }
const groupTitle = (g) => g[0].toUpperCase() + g.slice(1);
// table rows of the prompts `pids`, under a heading row per group; rowsOf(pids) makes the rows of some prompts
function groupedRows(pids, ncols, rowsOf) {
  return Object.keys(GROUP_INFO).flatMap((g) => {
    const ps = pids.filter((p) => promptGroup(p) === g);
    return ps.length ? [{ cls: "grp", span: ncols, cells: [`${groupTitle(g)} <span class="dim">&nbsp;(${ps.length}) - ` +
      `${GROUP_INFO[g]}</span>`] }, ...rowsOf(ps)] : [];
  });
}
// "10 performance · 15 quality" for the prompts of a run (single requests and long sessions)
function groupCounts(d) {
  const pids = [...new Set([...d.single.map((p) => p.prompt_id), ...Object.keys(d.sessions || {})])];
  return Object.keys(GROUP_INFO).map((g) => [g, pids.filter((p) => promptGroup(p) === g).length])
    .filter(([, n]) => n).map(([g, n]) => `${n} ${g}`).join(" · ");
}
