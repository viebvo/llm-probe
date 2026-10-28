"use strict";
// Compare - part of the page, loaded in the order of the <script> tags in index.html
// ------------------------------------------------------------------ compare
// compared runs, the first is the baseline (A); remembered in the browser
state.cmpRuns = (recall("cmp-runs") || "").split(",").filter(Boolean).map(Number);
state.ansRuns = (recall("ans-runs") || "").split(",").filter(Boolean).map(Number);
function setCompare(ids) {
  state.cmpRuns = [...new Set(ids)].slice(0, MAX_RUNS);
  store("cmp-runs", state.cmpRuns.join(","));
  renderPicker();
  renderCompare();
}

// one run picker, shared by Compare (min 2, with a baseline) and Responses (min 1); apply(ids) stores and re-renders
function runPicker(el, ids, apply, { min = 1, baseline = false, max = MAX_RUNS } = {}) {
  const known = new Set(state.runs.map((r) => r.id));
  ids = ids.filter((id) => known.has(id));
  const slots = ids.length < min ? [...ids, ...Array(min - ids.length).fill(null)] : ids;
  const options = state.runs.map((r) => `<option value="${r.id}">${esc(r.label)}</option>`).join("");
  el.innerHTML = slots.map((id, n) =>
    `<div class="pick"><b class="c${n}"${baseline ? tipAttr(n ? TIPS["cmp-b"] : TIPS["cmp-a"]) : ""}>${LETTERS[n]}${baseline && !n ? " (baseline)" : ""}</b>` +
    `<select data-n="${n}"><option value="">Select run</option>${options}</select>` +
    (n && slots.length > min ? `<button class="btn" data-del="${n}" title="Remove this run">x</button>` : "") +
    "</div>").join("") +
    (slots.length < max ? `<button class="btn pick-add" title="Add one more run (up to ${max})">+ Add run</button>` : "");
  el.querySelectorAll("select").forEach((sel) => {
    sel.value = slots[+sel.dataset.n] ?? "";
    sel.addEventListener("change", () => {
      const next = [...slots]; next[+sel.dataset.n] = sel.value ? +sel.value : null;
      apply(next.filter((x) => x != null));
    });
  });
  el.querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", () =>
    apply(ids.filter((_, n) => n !== +b.dataset.del))));
  const add = el.querySelector(".pick-add");
  if (add) add.addEventListener("click", () => {
    const free = state.runs.find((r) => !ids.includes(r.id));
    if (free) apply([...ids, free.id]);
  });
}
function renderPicker() {
  state.cmpRuns = state.cmpRuns.filter((id) => new Set(state.runs.map((r) => r.id)).has(id));
  runPicker($("cmp-pick"), state.cmpRuns, setCompare, { min: 2, baseline: true });
}
function renderAnsPicker() { runPicker($("ans-pick"), state.ansRuns, setAnsRuns, { min: 1, max: MAX_ANS_RUNS }); }
function setAnsRuns(ids) {
  if (!state.ansPreset) state.ansSet = null;  // other runs: all their prompts (a tile's "show relevant prompts" brings its own set)
  state.ansRuns = [...new Set(ids)].slice(0, MAX_ANS_RUNS);
  store("ans-runs", state.ansRuns.join(","));
  renderAnsPicker();
  renderResponses();
}

// compare tables: column grouping (by measurement or by run, remembered) and the row sort of each table (not kept)
state.cmpLayout = recall("cmp-layout") === "run" ? "run" : "metric";
state.cmpSort = {};
// a click on a sortable header (th[data-sort], see table()): sort that table by the column, again to reverse
function sortClick(e, store, redraw) {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const id = th.dataset.tbl, next = nextSort(store[id], th.dataset.sort);
  if (next.key) store[id] = next; else delete store[id];  // not sorted: the table's own order
  redraw();
}
// ---- Compare: highlight one run in every ranking and table. Hovering a row, a run letter or a table column highlights that
// run everywhere and fades the others; a click pins it (state.cmpPin = run id, kept over redraws and printed by Export PDF).
state.cmpPin = null; state.hlRuns = [];
const hlRoot = () => (state.report ? document.querySelector("main") : $("cmp-out"));
const hlIndex = () => state.hlRuns.findIndex((r) => r.d.run.id === state.cmpPin);
// tag what belongs to a run: table columns (header "TTFT A #84") and rows that start with the run's letter
function markRunCells(root) {
  const head = /(?:^|\s)([A-P]) #\d+$/;
  root.querySelectorAll("table").forEach((t) => {
    const ths = [...t.querySelectorAll("thead th")], cols = ths.map((th) => { const m = head.exec(th.textContent.trim()); return m ? LETTERS.indexOf(m[1]) : -1; });
    if (cols.some((i) => i >= 0)) {
      ths.forEach((th, c) => { if (cols[c] >= 0) th.dataset.run = cols[c]; });
      t.querySelectorAll("tbody tr").forEach((tr) => {
        if (tr.querySelector("td[colspan]")) return;
        [...tr.children].forEach((td, c) => { if (cols[c] >= 0) { td.dataset.run = cols[c]; if (!td.title && state.hlRuns[cols[c]]) td.title = runTipText(state.hlRuns[cols[c]]); } });
      });
    } else {
      t.querySelectorAll("tbody tr").forEach((tr) => {
        const sp = [...tr.querySelectorAll("td > span, td > b")].find((x) => /^c\d+$/.test(x.className)), n = sp ? +sp.className.slice(1) : -1, r = state.hlRuns[n];
        if (r && sp.textContent.trim().startsWith(r.L)) { tr.dataset.run = n; if (!tr.title) tr.title = runTipText(r); }
      });
    }
  });
}
function applyHl(idx) {
  state.hlCurrent = idx;
  const on = idx != null && idx >= 0;
  hlRoot().querySelectorAll("[data-run]").forEach((e) => {
    const me = on && +e.dataset.run === idx, cell = e.matches("td,th");
    e.classList.toggle("hl", me);
    e.classList.toggle(cell ? "hl-dimc" : "hl-dim", on && !me);
    e.classList.remove(cell ? "hl-dim" : "hl-dimc");
  });
}
function updateHlBar() {
  const bar = document.getElementById("hl-bar"), r = state.hlRuns[hlIndex()];
  if (!bar) return;
  bar.innerHTML = r ? `<span class="pinned"><b>Highlighted:</b> <b class="c${r.i}">${r.L}</b> = run #${r.d.run.id} · ${esc(r.d.run.model)} · ${esc(r.d.run.endpoint)} · ` +
      `started ${esc(String(r.d.run.started).slice(0, 16).replace("T", " "))}${state.report ? "" : ' <button class="btn small no-print" id="hl-release">release</button>'}</span>`
    : state.report ? "" : '<span class="dim no-print">Hover a run in any ranking, table or diagram to highlight it everywhere; a click pins it (Export PDF prints the pinned run highlighted).</span>';
}
function setPin(id) {
  state.cmpPin = id;
  if (hlIndex() < 0) state.cmpPin = null;  // the run is no longer part of the comparison
  updateHlBar();
  applyHl(state.cmpPin == null ? null : hlIndex());
}
{
  const out = $("cmp-out"), target = (e) => (e.target.closest ? e.target.closest("[data-run]") : null);
  out.addEventListener("mouseover", (e) => { if (state.cmpPin == null) { const t = target(e); applyHl(t ? +t.dataset.run : null); } });
  out.addEventListener("mouseleave", () => { if (state.cmpPin == null) applyHl(null); });
  out.addEventListener("click", (e) => {
    if (e.target.closest("#hl-release")) return setPin(null);
    if (e.target.closest("button, a, input, select, label, summary, th") || String(window.getSelection && window.getSelection()).trim()) return;
    const t = target(e);
    if (t) { const id = state.hlRuns[+t.dataset.run].d.run.id; setPin(state.cmpPin === id ? null : id); }
  });
}

function drawCompare() {
  const { c, runs } = state.cmp, y = window.scrollY;
  $("cmp-out").innerHTML = compareHtml(c, runs);
  markRunCells($("cmp-out"));
  setPin(state.cmpPin);
  rememberSections($("cmp-out"));
  if (!runs.every((r) => !hasMeasurements(r.d))) bindScaling("scaling-cmp", runs);
  window.scrollTo(0, y);  // a sort or layout change keeps the reading position
}
$("cmp-out").addEventListener("click", (e) => { if (state.cmp) sortClick(e, state.cmpSort, drawCompare); });
$("cmp-out").addEventListener("change", (e) => {
  const v = e.target.dataset && e.target.dataset.layout;
  if (!v || !state.cmp) return;
  state.cmpLayout = v;
  store("cmp-layout", v);
  drawCompare();
});
const renderCompare = () => busy(renderCompareNow);
async function renderCompareNow() {
  const ids = state.cmpRuns;
  $("cmp-pdf").disabled = $("cmp-show").disabled = ids.length < 2;
  if (ids.length < 2) {
    $("cmp-out").innerHTML = '<span class="dim">Select at least two runs (or mark several in Results and use "Compare marked").</span>';
    return;
  }
  const c = await api("/api/compare?runs=" + ids.join(","));
  const runs = compareRuns(c);
  state.cmp = { c, runs };
  const sel = ids.join(",");  // the list refresh re-renders an unchanged comparison: only a new selection resets
  if (state.cmpSel !== sel) { state.cmpSel = sel; state.cmpSort = {}; state.boxOpen.cmp = {}; }
  drawCompare();
}

function compareRuns(c) {
  const runs = c.runs.map((d, i) => ({ i, L: c.labels[i], name: `${c.labels[i]} #${d.run.id}`, d }));
  // headers like "TTFT A #84" get a tooltip naming the run (model + endpoint); see colTip()
  runColTip = Object.fromEntries(runs.map((r) => [r.name, `${r.d.run.model} · ${r.d.run.endpoint}`]));
  return runs;
}

// the comparison as HTML (Compare tab and printed report); c = /api/compare
function compareHtml(c, runs) {
  boxPage = "cmp";
  state.hlRuns = runs;
  const head = (r) => [`#${r.id}`, r.endpoint, r.model, "runtime " + runtimeLabel(r), "thinking " + r.thinking_label,
    "context " + r.context_label, r.placement_label === "-" ? "" : r.placement_label, r.gpu_info]
    .filter(Boolean).map(esc).join(" · ");
  const counts = (d) => groupCounts(d) ? ` · <span class="dim">prompts: ${esc(groupCounts(d))}</span>` : "";
  // every prompt some run has: the tables and time boxes list them all (- = not run), the key figures and the
  // average row only count the prompts all runs have, so they stay comparable
  const all = [...new Set(runs.flatMap((r) => r.d.single.map((s) => s.prompt_id)))];
  const partial = all.length > c.common.length
    ? `<br><span class="dim">Key figures and the average row use the ${c.common.length} prompts all runs have; the tables and time boxes list all ${all.length} (- = not run by that run).</span>` : "";
  let out = '<div class="hl-bar" id="hl-bar"></div><div class="meta">' + runs.map((r) => `<b class="c${r.i}">${r.L}</b> = ${head(r.d.run)}${counts(r.d)}`).join("<br>") + partial +
    `<br><span class="dim">Diff = deviation from ${runs[0].name} (baseline); green = better. ` +
    "TTFA = time to the first answer token (after the reasoning).</span></div>";
  if ((c.errors || []).length) {
    out += `<div class="err-box" title="The prompts of these runs differ: the same prompt id was not asked identically.">${c.errors.map(esc).join("<br>")}</div>`;
  }
  if ((c.notes || []).length) out += `<div class="dim" style="margin:0 0 10px;font-size:12.5px">${c.notes.map(esc).join("<br>")}</div>`;
  if (c.warnings.length) {
    out += `<div class="warn" title="The runs were not served the same way, so not every number is directly comparable.">` +
      `${c.warnings.map(esc).join("<br>")}</div>`;
  }
  if (c.capabilities.length) {
    out += box("caps", "Capability check", `<p class="hint">Hover a result for its detail.</p>` +
      capabilityTable(c.capabilities, runs));
  }
  if (runs.every((r) => !hasMeasurements(r.d))) return out;  // nothing measured
  out += kpiCards([...c.kpis, ...qualityKpis(runs)], runs);
  out += chartsRow("scaling-cmp", runs, all);

  const stats = runs.map((r) => Object.fromEntries(r.d.single.map((s) => [s.prompt_id, s])));
  // the columns of a table: one per measurement and run, grouped by measurement (TTFT A B C, TTFA A B C ...) or by
  // run (A: TTFT TTFA ..., B: ...); each column sorts the rows by its value when its header is clicked
  const cols = (metrics) => (state.cmpLayout === "run"
    ? runs.flatMap((r, n) => metrics.map((m) => ({ m, n }))) : metrics.flatMap((m) => runs.map((r, n) => ({ m, n }))));
  const heads = (cs) => cs.map(({ m, n }) => `${m.title} ${runs[n].name}`);
  const sortOf = (id, cs, first = "prompt", extra = 0) => ({ id, cur: state.cmpSort[id],
    keys: [first, ...cs.map(({ m, n }) => `${m.f}:${n}`), ...Array(extra).fill(null)] });
  const sorted = (id, items, valueOf) => {
    const st = state.cmpSort[id];
    return st ? sortBy(items, st, { [st.key]: (x) => valueOf(st.key, x) }) : items;
  };
  const layoutOpts = (id) => state.report ? "" : '<div class="box-opts no-print"><span>Columns grouped by:</span>' +
    ["metric", "run"].map((v) => `<label><input type="radio" name="cmp-layout-${id}" data-layout="${v}"` +
      `${state.cmpLayout === v ? " checked" : ""}>${v === "metric" ? "measurement" : "run"}</label>`).join("") +
    '<span>click a column header to sort the rows (again: reverse, again: original order)</span></div>';

  const smetrics = [{ f: "ttft", title: "TTFT", digits: 2, hb: false }, { f: "ttfa", title: "TTFA", digits: 2, hb: false },
    { f: "decode_tps", title: "tok/s", digits: 1, hb: true }, { f: "completion_tokens", title: "Tok.", digits: 0, plain: true }];
  const scols = cols(smetrics);
  const sval = (key, p) => { const [f, n] = key.split(":"); return key === "prompt" ? promptName(p) : stats[+n][p]?.[f]; };
  const rows = groupedRows(all, 1 + scols.length, (pids) => sorted("single", pids, sval).map((p) => [esc(promptName(p)) + ctxNote(runs, p),
    ...scols.map(({ m, n }) => !stats[n][p] ? '<span class="dim">-</span>' : m.plain ? fmt(stats[n][p][m.f], 0)
      : vd(stats.map((s) => s[p]?.[m.f] ?? null), n, m.digits, m.hb))]));
  if (c.common.length) {
    rows.push({ cls: "bold", cells: [all.length > c.common.length ? `Average (${c.common.length} common prompt${c.common.length === 1 ? "" : "s"})` : "Average", ...scols.map(({ m, n }) => m.plain ? "" :
      vd(stats.map((s) => avg(c.common.filter((p) => !isAdaptive(p)).map((p) => s[p][m.f]))), n, m.digits, m.hb))] });
  } else {
    rows.push([`<span class="dim">no common prompts</span>`, ...Array(scols.length).fill("")]);
  }
  out += section("cmp-single", "Single requests (median)", layoutOpts("single") +
    table("", ["Prompt", ...heads(scols)], rows, sortOf("single", scols)));

  const checked = all.filter((p) => stats.some((s) => s[p]?.check_n));
  if (checked.length) {
    const ccols = cols([{ f: "check_score", title: "Check" }]);
    const cval = (key, p) => key === "prompt" ? promptName(p) : stats[+key.split(":")[1]][p]?.check_score;
    out += section("cmp-check", "Answer check (share correct)", '<div class="box-opts no-print">' +
      "<span>click a column header to sort the rows (again: reverse, again: original order)</span></div>" + table("", ["Prompt", ...heads(ccols)],
      sorted("check", checked, cval).map((p) => [esc(promptName(p)) + ctxNote(runs, p), ...ccols.map(({ n }) => !stats[n][p] ? '<span class="dim">-</span>' :
        checkLabel(stats[n][p]) + (n ? ` <small>${delta(stats[0][p]?.check_score, stats[n][p].check_score)}</small>` : ""))]),
      sortOf("check", ccols)));
  }

  const loads = runs.map((r) => Object.fromEntries(r.d.load.map((l) => [l.concurrency, l])));
  const levels = [...new Set(loads.flatMap((x) => Object.keys(x)).map(Number))].sort((x, y) => x - y);
  const lmetrics = [{ f: "throughput", title: "Throughput", digits: 1, hb: true },
    { f: "latency_p95", title: "p95 latency", digits: 1, hb: false, unit: " s" },
    { f: "ttft_avg", title: "Avg TTFT", digits: 2, plain: true, unit: " s" }];
  const lcols = cols(lmetrics);
  const lval = (key, lv) => key === "prompt" ? lv : (loads[+key.split(":")[1]][lv] || {})[key.split(":")[0]];
  out += section("cmp-load", "Concurrent load", layoutOpts("load") + table("", ["Concurrency", ...heads(lcols), "Errors"],
    sorted("load", levels, lval).map((lv) => {
      const v = (f) => loads.map((x) => (x[lv] ? x[lv][f] : null));
      return [lv, ...lcols.map(({ m, n }) => m.plain ? fmt(v(m.f)[n], m.digits, m.unit)
        : vd(v(m.f), n, m.digits, m.hb, m.unit || "")), v("errors").map((e) => e || 0).join("/")];
    }), sortOf("load", lcols, "prompt", 1)));
  out += tokenSection("cmp-tok", runs);
  out += sessionSection("cmp-sess", runs);
  return out;
}

