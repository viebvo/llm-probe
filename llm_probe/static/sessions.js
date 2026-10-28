"use strict";
// long sessions (deviation) - part of the page, loaded in the order of the <script> tags in index.html
// ---------------------------------------------------------------- long sessions (deviation)
const pct = (ok, n) => (n ? ok / n * 100 : null);
function stepShare(s) { return pct((s.new_ok || 0) + (s.earlier_ok || 0), (s.new_n || 0) + (s.earlier_n || 0)); }

// lines over the session steps: [{name, color (css var), values: [value or null per step]}]
// The line charts are drawn at the real pixel width of their box (not scaled like a picture), so text, lines and markers keep
// the size of the rest of the page at every window width: fitChart() puts a placeholder with its draw(width) function in the
// page, a ResizeObserver (see below) draws it at the width it gets and redraws when the box changes (window, collapsing).
const CHART_DRAW = {};
let chartSeq = 0;
function fitChart(draw, w0 = 600) {
  const id = "ch" + ++chartSeq;
  CHART_DRAW[id] = draw;
  delete CHART_DRAW["ch" + (chartSeq - 80)];  // the redraw functions of old pages are not needed any more
  return `<div class="chartfit" data-fit="${id}">${draw(w0)}</div>`;
}
const chartRO = new ResizeObserver((entries) => entries.forEach(({ target: el }) => {
  const w = Math.round(el.clientWidth), draw = CHART_DRAW[el.dataset.fit];
  if (draw && w >= 200 && Math.abs(w - (+el.dataset.w || 0)) > 6) { el.dataset.w = w; el.innerHTML = draw(w); if (state.hlCurrent != null && typeof applyHl === "function") applyHl(state.hlCurrent); }
}));
new MutationObserver(() => document.querySelectorAll(".chartfit:not([data-ro])").forEach((el) => { el.dataset.ro = 1; chartRO.observe(el); }))
  .observe(document.body, { childList: true, subtree: true });
const chartH = (w, lo, hi, k) => Math.round(Math.min(hi, Math.max(lo, w * k)));
function stepChart(lines, n, ymax = 100, unit = " %") {
  const step = niceStep(ymax);
  ymax = Math.ceil(ymax / step) * step || 1;
  return fitChart((W) => stepChartSvg(W, lines, n, ymax, step, unit), 560);
}
function stepChartSvg(W, lines, n, ymax, step, unit) {
  const H = chartH(W, 200, 260, 0.4), L = 46, R = 14, T = 10, B = 32;
  const x = (k) => L + (n === 1 ? (W - L - R) / 2 : k * (W - L - R) / (n - 1));
  const y = (v) => T + (H - T - B) * (1 - v / ymax);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
  for (let v = 0; v <= ymax + 1e-9; v += step) {
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${+v.toFixed(1)}</text>`;
  }
  for (let k = 0; k < n; k++) svg += `<text x="${x(k)}" y="${H - B + 16}" text-anchor="middle">${k + 1}</text>`;
  svg += `<text x="${(L + W - R) / 2}" y="${H - 3}" text-anchor="middle">step</text>`;
  for (const l of lines) {
    const pts = l.values.map((v, k) => [k, v]).filter(([, v]) => v != null);
    svg += `<g${l.run != null ? ` data-run="${l.run}"` : ""}><polyline fill="none" stroke="${l.color}" stroke-width="2" points="${pts.map(([k, v]) => `${x(k)},${y(v)}`).join(" ")}"/>` +
      pts.map(([k, v]) => `<circle cx="${x(k)}" cy="${y(v)}" r="4" fill="${l.color}"><title>${esc(`${l.name}, step ${k + 1}: ${fmt(v, unit === " %" ? 0 : 0, unit)}`)}</title></circle>`).join("") + "</g>";
  }
  return svg + "</svg>";
}

function sessionSummaryText(x) {
  const s = x.summary;
  return `score ${fmt(s.score == null ? null : s.score * 100, 0, " %")} · last step ${fmt(s.final == null ? null : s.final * 100, 0, " %")} · ` +
    (s.regressions ? `<span class="yellow">regressions in ${s.regressions} step(s), first in step ${s.first_regression}</span>` : '<span class="green">no regressions</span>') +
    (s.rule_steps ? ` · <span class="yellow">rules broken in ${s.rule_steps} step(s)</span>` : "") +
    ` · first half ${fmt(s.early == null ? null : s.early * 100, 0, " %")}, second half ${fmt(s.late == null ? null : s.late * 100, 0, " %")}` +
    ` · prompt tokens ${fmt(s.prompt_tokens_first, 0)} → ${fmt(s.prompt_tokens_last, 0)}` +
    (s.done < s.steps ? ` · <span class="yellow">stopped after ${s.done}/${s.steps} steps</span>` : "");
}

function sessionSection(key, runs) {
  const pids = [...new Set(runs.flatMap((r) => Object.keys(r.d.sessions || {})))];
  if (!pids.length) return "";
  const one = runs.length === 1;
  // the run, written out (so nothing needs a mouse-over): "A #111 gemma2:27b · ollama-ct"
  const runName = (r) => `<b class="c${r.i}"${runTip(r)}>${esc(r.name)}</b> ${esc(r.d.run.model)} <span class="dim">· ${esc(r.d.run.endpoint)}</span>`;
  let out = '<p class="hint">Long sessions (deviation tests): the model develops a module step by step; after every step its code runs ' +
    'in a sandbox against all tests valid at that point. <i>New</i> = tests of the step\'s request, <i>earlier</i> = everything ' +
    "requested before - failing earlier tests are regressions (the model drifts away). Hover a point for its value.</p>";
  for (const pid of pids) {  // one box per session prompt: runs and charts, and its result list directly below
    const p = state.prompts.find((x) => x.id === pid) || { name: pid, steps: [] };
    const have = runs.filter((r) => (r.d.sessions || {})[pid]);
    const n = Math.max(p.steps.length, ...have.map((r) => r.d.sessions[pid].summary.steps));
    let body = have.map((r) => `<div data-run="${r.i}">${one ? "" : `${runName(r)}: `}<span class="dim">${sessionSummaryText(r.d.sessions[pid])}</span></div>`).join("");
    const lines = one
      ? [{ name: "new tests", color: "var(--c0)", values: steps(have[0], pid, n).map((s) => s && pct(s.new_ok, s.new_n)) },
         { name: "earlier tests", color: "var(--c1)", values: steps(have[0], pid, n).map((s) => s && pct(s.earlier_ok, s.earlier_n)) }]
      : have.map((r) => ({ name: r.name, run: r.i, color: `var(--c${r.i})`, values: steps(r, pid, n).map((s) => s && stepShare(s)) }));
    const tok = have.map((r) => ({ name: r.name, run: r.i, color: `var(--c${r.i})`, values: steps(r, pid, n).map((s) => s && s.prompt_tokens) }));
    const tmax = Math.max(...tok.flatMap((l) => l.values.filter((v) => v != null)), 1);
    body += `<div class="grid2 charts"><div class="card"><h2>${one ? "Tests passed per step: new / earlier" : "All tests passed per step"} (%)</h2>` +
      `${stepChart(lines, n)}${one ? '<div class="dim"><span class="c0">&#9632;</span> new tests &nbsp; <span class="c1">&#9632;</span> earlier tests (regressions)</div>' : ""}</div>` +
      `<div class="card"><h2>Prompt tokens per step (the context grows)</h2>${stepChart(tok, n, tmax, " tok")}</div></div>`;
    const rows = [];
    for (let k = 0; k < n; k++) {
      for (const r of have) {
        const s = steps(r, pid, n)[k];
        if (!s) continue;
        const failed = Object.entries(s.failed || {}).map(([t, v]) => `${t}: ${v}`).join("\n");
        rows.push([one || r === have[0] ? `<span title="${esc(p.steps[k] || "")}">${k + 1}</span>` : "",
          ...(one ? [] : [`<b class="c${r.i}"${runTip(r)}>${esc(r.L)}</b> <span class="dim">${esc(r.d.run.model)}</span>`]),
          s.new_n ? `<span class="${s.new_ok < s.new_n ? "red" : "green"}">${s.new_ok}/${s.new_n}</span>` : '<span class="dim">-</span>',
          s.earlier_n ? `<span class="${s.earlier_ok < s.earlier_n ? "yellow" : "green"}">${s.earlier_ok}/${s.earlier_n}</span>` : '<span class="dim">-</span>',
          s.rules && s.rules.length ? `<span class="yellow" title="${esc(s.rules.join("\n"))}">${s.rules.length}</span>` : "",
          `<span class="cap-detail" title="${esc(failed)}">${esc(s.request_error || s.error || (failed ? Object.keys(s.failed).join(", ") : ""))}</span>`,
          fmt(s.prompt_tokens, 0), fmt(s.completion_tokens, 0) + (s.cut ? ' <span class="yellow" title="cut off at max_tokens">!</span>' : ""),
          fmt(s.ttft, 2, " s"), fmt(s.total, 1, " s")]);
      }
    }
    body += box(`${key}-${pid}-list`, "Results per step", table("", ["Step", ...(one ? [] : ["Run"]), "New tests", "Earlier tests", "Rules broken", "Failed / error",
      "Prompt tok.", "Output tok.", "TTFT", "Duration"], rows), { sub: true, closed: true });
    out += box(`${key}-${pid}`, `Long session: ${esc(p.name)}`, body);
  }
  return out;
}
function steps(r, pid, n) {
  const byStep = Object.fromEntries(((r.d.sessions || {})[pid] || { steps: [] }).steps.map((s) => [s.step, s]));
  return Array.from({ length: n }, (_, k) => byStep[k + 1] || null);
}

// the top row of a run / comparison: scaling under load next to the resources (compare with 5+ runs: 1/3 and 2/3,
// the resource tables need the width), then "where the time goes"
function chartsRow(key, runs, prompts) {
  return `<div class="row2${runs.length > 4 ? " r13" : ""}">${scalingCard(key, runs)}${resourcesBox(runs)}</div>` +
    sweepBox(runs) + timeCard(runs, prompts);
}
// The context sweep: the log triage at growing sizes; per run the share found at each size, and the usable length (the
// largest size passed before the first one that failed; a size passes at 2/3 correct)
function sweepBox(runs) {
  const data = runs.map((r) => Object.values(r.d.sweep || {})[0] || null);
  if (!data.some(Boolean)) {  // always there, grey and empty: so the runs keep their places
    return box("sweep", 'Context sweep <span class="dim">&nbsp;- not run</span>', '<p class="hint">Not run - tick the context sweep on the Test page ' +
      "(off by default; it takes minutes, up to half an hour for a large context). It tests the log triage at growing prompt sizes and " +
      "finds the length up to which the model still finds the incidents.</p>", { cls: "off" });
  }
  const sizes = [...new Set(data.flatMap((d) => (d ? d.sizes.map((x) => x.size) : [])))].sort((a, b) => a - b);
  const draw = (W) => {
  const H = chartH(W, 240, 340, 0.5), L = 46, R = 16, T = 22, B = 36, x = (k) => L + (sizes.length === 1 ? (W - L - R) / 2 : k * (W - L - R) / (sizes.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / 100);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
  for (const v of [0, 25, 50, 75, 100]) svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  svg += `<line x1="${L}" x2="${W - R}" y1="${y(200 / 3)}" y2="${y(200 / 3)}" stroke="var(--muted)" stroke-dasharray="4 4"/>` +
    `<text x="${W - R}" y="${y(200 / 3) - 4}" text-anchor="end">pass: 2/3</text><text x="${L - 6}" y="${T - 10}" text-anchor="end">%</text>`;
  sizes.forEach((sz, k) => { svg += `<text x="${x(k)}" y="${H - B + 16}" text-anchor="middle">${fmtK(sz)}</text>`; });
  svg += `<text x="${(L + W - R) / 2}" y="${H - 4}" text-anchor="middle">prompt size (tokens)</text>`;
  runs.forEach((r, n) => {
    const d = data[n];
    if (!d) return;
    const pts = d.sizes.filter((v) => v.score != null).map((v) => [sizes.indexOf(v.size), v]);
    svg += `<g data-run="${r.i}"><polyline fill="none" stroke="var(--c${r.i})" stroke-width="2" points="${pts.map(([k, v]) => `${x(k)},${y(v.score * 100)}`).join(" ")}"/>`;
    svg += pts.map(([k, v]) => {
      const cx = x(k), cy = y(v.score * 100), tip = `<title>${esc(`${r.L ? r.name + " " : ""}${r.d.run.model}, ${fmtK(v.size)} tokens: ${fmt(v.score * 100, 0, " %")} correct (${v.testable} of ${v.n} testable)`)}</title>`;
      return r.i >= 8 ? `<polygon points="${cx},${cy - 5.5} ${cx + 5.5},${cy} ${cx},${cy + 5.5} ${cx - 5.5},${cy}" fill="var(--c${r.i})">${tip}</polygon>`
        : `<circle cx="${cx}" cy="${cy}" r="4.5" fill="var(--c${r.i})">${tip}</circle>`;
    }).join("") + "</g>";
  });
  return svg + "</svg>";
  };
  const cell = (v) => (!v ? '<span class="dim">-</span>' : v.score == null ? '<span class="dim" title="every response was cut off - neither pass nor fail">not testable</span>'
    : `<span class="${v.passed ? "green" : "red"}">${fmt(v.score * 100, 0, " %")}</span> <span class="dim">(${v.testable}/${v.n})</span>`);
  const rows = sizes.map((sz) => [fmtK(sz) + " tokens", ...data.map((d) => cell(d && d.sizes.find((v) => v.size === sz)))]);
  rows.push({ cls: "bold", cells: ["Usable context", ...data.map((d) => (!d ? "-" : d.usable ? `${fmtK(d.usable)} <span class="dim">of ${fmtK(d.context)}</span>` : '<span class="red">none</span>'))] });
  const sum = runs.length === 1 && data[0] ? ` <span class="dim">&nbsp;- usable ${data[0].usable ? fmtK(data[0].usable) : "none"} of ${fmtK(data[0].context)}</span>` : "";
  return box("sweep", "Context sweep" + sum, '<p class="hint">The log triage at growing sizes: how much of the incidents the model finds at each prompt size. A size passes at 2/3 ' +
    "correct; the usable context is the largest size passed before the first one that failed. The sweep stops after two failed sizes in a row; " +
    'responses cut off at max_tokens are "not testable".</p><div class="row2">' + `<div>${fitChart(draw)}</div>` + `<div>${table("", ["Prompt size", ...runs.map((r) => r.name)], rows)}</div></div>`);
}
function resourcesBox(runs) {
  if (!runs.some((r) => (r.d.resources || []).length)) {
    return box("res", 'Resources <span class="dim">&nbsp;- no data</span>', '<p class="hint">No resource data - set an SSH host on the endpoint (Test tab), ' +
      "then GPU load, VRAM, power, temperature, CPU and RAM of the server are recorded during the test.</p>", { cls: "res off" });
  }
  const hint = '<p class="hint">Across all GPUs: utilization averaged, VRAM and power summed.</p>';
  if (runs.length === 1) return box("res", "Resources", hint + resourceTable("", runs[0].d.resources), { cls: "res" });
  // a comparison: per phase one block, per resource one row, one column per run
  const gb = (mb) => (mb == null ? null : mb / 1024);
  const kinds = [["GPU utilization, avg %", (x) => fmt(x.gpu_util_avg, 0)], ["GPU utilization, max %", (x) => fmt(x.gpu_util_max, 0)],
    ["GPU balance", gpuBalance], ["VRAM, max", (x) => (x.vram_max_mb == null ? "-" : `${gb(x.vram_max_mb).toFixed(1)}/${gb(x.vram_total_mb || 0).toFixed(0)} GB`)],
    ["Power, avg W", (x) => fmt(x.power_avg, 0)], ["Power, max W", (x) => fmt(x.power_max, 0)], ["Temperature, max °C", (x) => fmt(x.temp_max, 0)],
    ["CPU, avg %", (x) => fmt(x.cpu_avg, 0)], ["RAM, max", (x) => (x.mem_max_mb ? fmt(gb(x.mem_max_mb), 1, " GB") : "-")]];
  const byPhase = runs.map((r) => Object.fromEntries((r.d.resources || []).map((x) => [x.phase, x])));
  const rows = [];
  for (const phase of ["single", "load", "total"]) {
    if (!byPhase.some((p) => p[phase])) continue;
    rows.push({ cls: "grp", span: runs.length + 1, cells: [`${esc(PHASES[phase] || phase)}`] });
    for (const [label, f] of kinds) rows.push([esc(label), ...byPhase.map((p) => (p[phase] ? f(p[phase]) : '<span class="dim">-</span>'))]);
  }
  return box("res", "Resources", hint + (rows.length ? table("", ["Resource", ...runs.map((r) => r.name)], rows) : '<p class="dim">no data</p>'), { cls: "res" });
}

function niceStep(max, ticks = 4) {
  const raw = max / ticks, mag = 10 ** Math.floor(Math.log10(raw)), n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

// the load metrics: tok/s ones share the left axis, the latency (seconds) gets the right one; one run = one colour per
// metric, several runs = one colour per run and a line style per metric
const SCALING = {
  throughput: { label: "Throughput (tok/s)", name: "Throughput", unit: " tok/s", digits: 1, axis: "tps", color: "var(--c0)", dash: "", tip: COL["Throughput tok/s"] },
  tps_per_req: { label: "tok/s per request", name: "tok/s per request", unit: " tok/s", digits: 1, axis: "tps", color: "var(--c1)", dash: "7 4", tip: COL["tok/s per req"] },
  latency_p95: { label: "p95 latency (sec)", name: "p95 latency", unit: " sec", digits: 1, axis: "sec", color: "var(--c2)", dash: "2 4", tip: COL["p95 latency"] },
};
// the metrics shown: remembered per page; by default all, but only throughput for a comparison of 4+ runs
function scalingMetrics(key, runs) {
  const all = Object.keys(SCALING);
  if (state.report) return all;
  try { const v = JSON.parse(recall("scaling:" + key)); if (Array.isArray(v)) return v.filter((k) => k in SCALING); } catch (e) { /* default */ }
  return runs.length > 3 ? ["throughput"] : all;
}
function scalingChart(runs, metrics) {
  const ms = Object.keys(SCALING).filter((k) => metrics.includes(k));
  if (!ms.length) return '<p class="dim">Tick at least one measurement.</p>';
  const data = runs.map((r) => Object.fromEntries(r.d.load.map((l) => [l.concurrency, l])));
  const levels = [...new Set(data.flatMap((x) => Object.keys(x)).map(Number))].sort((x, y) => x - y);
  if (!levels.length) return '<p class="dim">No concurrent load phase.</p>';
  const hasTps = ms.some((k) => SCALING[k].axis === "tps"), hasSec = ms.some((k) => SCALING[k].axis === "sec");
  const sideOf = (ax) => (ax === "tps" || !hasTps ? "L" : "R");  // a latency-only chart uses the left axis
  const multi = runs.length > 1;
  const axes = {};
  for (const ax of ["tps", "sec"]) {
    const vals = ms.filter((k) => SCALING[k].axis === ax).flatMap((k) => data.flatMap((x) => Object.values(x).map((l) => l[k] ?? 0)));
    if (!vals.length) continue;
    const vmax = Math.max(...vals) || 1, step = niceStep(vmax);
    axes[ax] = { step, ymax: Math.ceil(vmax / step) * step, unit: ax === "tps" ? "tok/s" : "sec" };
  }
  const draw = (W) => {
  const H = chartH(W, 240, 340, 0.5), L = 52, R = hasTps && hasSec ? 50 : 16, T = 24, B = 34;
  const x = (n) => L + (levels.length === 1 ? (W - L - R) / 2 : n * (W - L - R) / (levels.length - 1));
  const y = (ax, v) => T + (H - T - B) * (1 - v / axes[ax].ymax);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
  for (const [ax, a] of Object.entries(axes)) {
    const left = sideOf(ax) === "L";
    for (let v = 0; v <= a.ymax + 1e-9; v += a.step) {
      if (left) svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(ax, v)}" y2="${y(ax, v)}"/>`;
      svg += `<text x="${left ? L - 6 : W - R + 6}" y="${y(ax, v) + 4}" text-anchor="${left ? "end" : "start"}">${+v.toFixed(2)}</text>`;
    }
    svg += `<text x="${left ? L - 6 : W - R + 6}" y="${T - 10}" text-anchor="${left ? "end" : "start"}">${a.unit}</text>`;
  }
  levels.forEach((c, n) => { svg += `<text x="${x(n)}" y="${H - B + 16}" text-anchor="middle">${c}</text>`; });
  svg += `<text x="${(L + W - R) / 2}" y="${H - 4}" text-anchor="middle">concurrent requests</text>`;
  runs.forEach((r, n) => ms.forEach((k) => {
    const m = SCALING[k], color = multi ? `var(--c${r.i})` : m.color;
    const pts = levels.map((c, j) => [c, j, data[n][c] ? data[n][c][k] : null]).filter(([, , v]) => v != null);
    svg += `<g data-run="${r.i}"><polyline fill="none" stroke="${color}" stroke-width="2"${multi && m.dash ? ` stroke-dasharray="${m.dash}"` : ""} ` +
      `points="${pts.map(([, j, v]) => `${x(j)},${y(m.axis, v)}`).join(" ")}"/>`;
    svg += pts.map(([c, j, v]) => {
      const cx = x(j), cy = y(m.axis, v), tip = `<title>${esc(`${r.name || "run"} ${r.d.run.model}, ${c} concurrent: ${m.name} ${fmt(v, m.digits, m.unit)}`)}</title>`;
      return r.i >= 8 ? `<polygon points="${cx},${cy - 5.5} ${cx + 5.5},${cy} ${cx},${cy + 5.5} ${cx - 5.5},${cy}" fill="${color}">${tip}</polygon>`
        : `<circle cx="${cx}" cy="${cy}" r="4.5" fill="${color}">${tip}</circle>`;
    }).join("") + "</g>";
  }));
  return svg + "</svg>";
  };
  const line = (color, dash) => `<svg viewBox="0 0 26 8"><line x1="0" x2="26" y1="4" y2="4" stroke="${color}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ""}/></svg>`;
  const legend = '<div class="sc-legend">' + (multi
    ? ms.map((k) => `<span>${line("var(--muted)", SCALING[k].dash)}${esc(SCALING[k].label)}</span>`).join("") +
      runs.map((r) => `<span class="c${r.i}" data-run="${r.i}"${runTip(r)}>${r.i >= 8 ? "&#9670;" : "&#9679;"} ${esc(r.L)}</span>`).join("")
    : ms.map((k) => `<span>${line(SCALING[k].color, "")}${esc(SCALING[k].label)}</span>`).join("")) + "</div>";
  return fitChart(draw) + legend;
}

function scalingCard(key, runs) {
  if (!runs.some((r) => r.d.load.length)) {  // grey and empty, like the context sweep: the boxes keep their places
    return box("scaling", 'Scaling under load <span class="dim">&nbsp;- not run</span>', '<p class="hint">Not run - tick "Concurrent load" on the Test page. ' +
      "It sends requests in parallel (1, 2, 4, 8 ...) and shows how throughput, speed per request and latency change with the load.</p>", { cls: "off" });
  }
  const sel = scalingMetrics(key, runs);
  return box("scaling", "Scaling under load", (state.report ? "" : '<div class="box-opts no-print">' +
    Object.entries(SCALING).map(([k, m]) => `<label title="${esc(m.tip)}"><input type="checkbox" name="${key}" value="${k}"` +
      `${sel.includes(k) ? " checked" : ""}>${esc(m.label)}</label>`).join("") + "</div>") +
    `<div id="${key}">${scalingChart(runs, sel)}</div>`);
}
function bindScaling(key, runs) {
  document.querySelectorAll(`input[name=${key}]`).forEach((c) => c.addEventListener("change", () => {
    const sel = [...document.querySelectorAll(`input[name=${key}]:checked`)].map((i) => i.value);
    store("scaling:" + key, JSON.stringify(sel));
    $(key).innerHTML = scalingChart(runs, sel);
    if (state.hlCurrent != null) applyHl(state.hlCurrent);  // (a pinned run stays highlighted in the redrawn chart)
  }));
}

// stacked bar of one response or median: time to first token, reasoning, answer
// one stacked bar: time to first token, reasoning (up to the first answer token), the answer; h = height in px.
// No answer token (ttfa null): the response ended in the reasoning - cut off at the time limit or at max_tokens, or it
// produced nothing - so everything after the first token is drawn HATCHED as "no answer", never as an answer.
function segBar(t, scale, color, h, label = "") {
  if (!t || t.total == null) return `<div class="segs" style="height:${h}px"></div>`;
  const ttft = t.ttft ?? 0, pct = (v) => `${(Math.max(0, v) / scale * 100).toFixed(2)}%`;
  const first = `<div class="seg-ttft" style="width:${pct(ttft)}" title="${label}Time to first token: ${fmt(ttft, 2)} s"></div>`;
  if (t.ttfa == null) {
    const cut = t.cut || (t.cut_time ? "time" : t.cut_tokens ? "tokens" : "");
    const why = cut === "time" ? "cut off at the time limit while reasoning" : cut === "tokens" ? "cut off at max_tokens while reasoning" : "no answer (only reasoning)";
    return `<div class="segs" style="height:${h}px">${first}<div class="seg-cut" style="width:${pct(t.total - ttft)}" ` +
      `title="${label}${why} - no answer: ${fmt(t.total - ttft, 2)} s after the first token (total ${fmt(t.total, 2)} s)"></div></div>`;
  }
  const ttfa = t.ttfa;
  return `<div class="segs" style="height:${h}px">${first}` +
    (ttfa - ttft >= 0.005 ? `<div class="seg-think" style="width:${pct(ttfa - ttft)}" title="${label}Reasoning: ${fmt(ttfa - ttft, 2)} s (answer after ${fmt(ttfa, 2)} s)"></div>` : "") +
    `<div class="f${color}" style="width:${pct(t.total - ttfa)}" title="${label}answer: ${fmt(t.total - ttfa, 2)} s (total ${fmt(t.total, 2)} s)"></div></div>`;
}
// The bar of one run and prompt: the median (t) as one bar, or - with 2+ repetitions - one thin bar per repetition,
// and from 3 repetitions a light bar behind them = the median of the repetitions, so a slow or fast one stands out.
function timeSegs(t, scale, color, reps = []) {
  if (reps.length < 2) return `<div class="segs-wrap">${segBar(t, scale, color, 12)}</div>`;
  const h = Math.max(3, Math.min(8, Math.floor(20 / reps.length))), med = median(reps.map((x) => x.total));
  const ghost = reps.length < 3 ? "" : `<div class="ghost" style="width:${(Math.max(0, med) / scale * 100).toFixed(2)}%" ` +
    `title="Median of the ${reps.length} repetitions: ${fmt(med, 1)} s"></div>`;
  return `<div class="segs-wrap">${ghost}${reps.map((x, k) => segBar(x, scale, color, h, `Repetition ${k + 1}: `)).join("")}</div>`;
}
function median(values) {
  const v = values.filter((x) => x != null).sort((x, y) => x - y), m = v.length >> 1;
  return v.length ? (v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2) : null;
}

// per-prompt overall result for a run, shown left of its time bar: green = every check passed, red = at least one
// answer wrong, amber "!" = at least one error (a failed or empty response), grey box = a speed test (not checked)
const RESULT_BOX = { ok: ["ok", "✓", "all checks passed"], bad: ["bad", "✗", "at least one answer wrong"],
                     err: ["unk", "!", "at least one error (a failed or empty response)"],
                     none: ["none", "", "speed test - the answer is not checked"],
                     trunc: ["unk", "!", "the server cut the prompt (context per request too small) - not counted"],
                     und: ["none", "?", "not counted: the whole prompt was processed, so there was no overflow to judge"] };
function resultBox(res) {
  const [cls, ch, why] = RESULT_BOX[res] || RESULT_BOX.none;
  return `<span class="cbox ${cls}" title="${esc(why)}">${ch}</span>`;
}
function promptResult(stat, checks, pid) {
  if (promptGroup(pid) !== "quality") return "none";  // performance/speed prompt: not checked
  const s = stat[pid] || {}, cs = checks[pid] || [];
  if (cs.some((c) => c.cut === "context")) return "trunc";
  if (cs.length && cs.every((c) => c.undecided) && (lookupPrompt(pid) || {}).adaptive === "overflow") return "und";
  const wrong = cs.some((c) => c.answered && !c.undecided &&
    ((c.total && c.correct < c.total) || (c.smoke_total && c.smoke_pass < c.smoke_total)));
  if (wrong) return "bad";
  if ((s.errors || 0) > 0 || cs.some((c) => !c.answered) || !cs.length) return "err";
  return "ok";
}
// the themes of "where the time goes", each a sub box: the speed tests, then the checked prompts by type
const TIME_THEMES = [
  { id: "performance", title: "Performance", note: "speed tests, answers not checked", of: (p) => promptGroup(p) === "performance" },
  ...[["text", "text"], ["vision", "vision and OCR"], ["tools", "tool calling"], ["safety", "safety (refusal tests)"]].map(([k, label]) => (
    { id: "q-" + k, title: "Quality: " + label, note: "answers checked", of: (p) => promptGroup(p) === "quality" && promptKind(p) === k })),
  { id: "q-other", title: "Quality: other", note: "answers checked",
    of: (p) => promptGroup(p) === "quality" && !["text", "vision", "tools", "safety"].includes(promptKind(p)) },
  { id: "reliability", title: "Reliability", note: "faithful to the source, failures announced", of: (p) => promptGroup(p) === "reliability" },
];
function promptKind(id) { return (lookupPrompt(id) || {}).kind || "text"; }
function timeBreakdown(runs, prompts) {
  if (!prompts.length) return '<p class="dim">No prompts.</p>';
  const stats = runs.map((r) => Object.fromEntries(r.d.single.map((s) => [s.prompt_id, s])));  // a run may lack a prompt
  const repsOf = (n, p) => ((runs[n].d.reps || {})[p] || []).filter((x) => x.total != null);
  const scale = Math.max(...prompts.flatMap((p) => stats.flatMap((s, n) => [s[p]?.total ?? 0, ...repsOf(n, p).map((x) => x.total)]))) || 1;
  const legend = '<div class="legend"><span><i class="seg-ttft"></i>time to first token (sec)</span>' +
    '<span><i class="seg-think"></i>reasoning (sec)</span>' +
    '<span title="The response ended in the reasoning: cut off at the time limit or at max_tokens, no answer token ever came. A prompt\'s median bar is hatched like this only when none of its repetitions got to an answer; the thin bars show each repetition."><i class="seg-cut"></i>cut off while reasoning - no answer</span><span>' + runs.map((r) => `<i class="f${r.i}"></i>`).join("") +
    `answer (sec)${runs.length > 1 ? " - " + runs.map((r) => r.L).join(" / ") : ""}</span>` +
    '<span><i class="ghost-key"></i>median of the repetitions (background; one thin bar per repetition)</span>' +
    (runs.length >= 3 ? '<span><i class="ghost-key"></i>in the overview: median of all runs</span>' : "") + "</div>";
  const bars = (ps) => ps.map((p) => `<div class="tb-prompt">${esc(promptName(p))}${ctxNote(runs, p)}</div>` + runs.map((r, n) => !stats[n][p]
    ? `<div class="tbar" data-run="${r.i}"><span class="c${r.i}"${runTip(r)}>${r.L}</span><span></span><span class="dim">not run</span><span></span><span></span></div>` :
    `<div class="tbar" data-run="${r.i}"><span class="c${r.i}"${runTip(r)}>${r.L}</span>` +
      `${resultBox(promptResult(stats[n], r.d.checks || {}, p))}` +
      `${timeSegs(stats[n][p], scale, r.i, repsOf(n, p))}<span title="median of the repetitions">${fmt(stats[n][p].total, 1, " s")}</span>` +
      `<button class="btn small no-print" title="Open this run's responses for this prompt (all repetitions) in the Responses tab" ` +
      `onclick="showAnswerFor(${r.d.run.id}, &quot;${esc(p)}&quot;)">show answer(s)</button></div>`).join("")).join("");
  const themes = TIME_THEMES.map((t) => ({ t, ps: prompts.filter(t.of) })).filter((x) => x.ps.length);
  // overview: per theme and run the median over the theme's prompts
  const overview = () => {
    const rows = themes.map(({ t, ps }) => ({ t, med: runs.map((r, n) => ({
      ttft: median(ps.map((p) => stats[n][p]?.ttft)), ttfa: median(ps.map((p) => stats[n][p]?.ttfa)), total: median(ps.map((p) => stats[n][p]?.total)) })) }));
    const oscale = Math.max(...rows.flatMap((x) => x.med.map((m) => m.total ?? 0))) || 1;
    // one continuous light bar behind all runs of a theme = the median of the runs' medians (3+ runs; for 2 it says nothing)
    return rows.map(({ t, med }) => {
      const m = runs.length >= 3 ? median(med.map((x) => x.total)) : null;
      const ghost = m == null ? "" : `<div class="ghost ovghost" style="grid-area:1 / 3 / span ${runs.length} / span 1;width:${(Math.max(0, m) / oscale * 100).toFixed(2)}%" ` +
        `title="Median of all runs: ${fmt(m, 1)} s"></div>`;
      return `<div class="tb-group">${esc(t.title)}</div><div class="ovblock">${ghost}` + runs.map((r, n) =>
        `<span class="c${r.i}" data-run="${r.i}" style="grid-area:${n + 1} / 1"${runTip(r)}>${r.L}</span><div data-run="${r.i}" style="grid-area:${n + 1} / 3">${timeSegs(med[n], oscale, r.i)}</div>` +
        `<span data-run="${r.i}" style="grid-area:${n + 1} / 4">${fmt(med[n].total, 1, " s")}</span>`).join("") + "</div>";
    }).join("");
  };
  const tiles = [box("time-overview", `Overview <span class="dim">&nbsp;- median per theme${runs.length >= 3 ? "; light bar = median of all runs" : ""}</span>`, overview(), { sub: true })].concat(
    themes.map(({ t, ps }) => box("time-" + t.id, `${esc(t.title)} <span class="dim">&nbsp;(${ps.length}) - ${t.note}</span>`, bars(ps), { sub: true })));
  return legend + `<div class="time-grid">${tiles.join("")}</div>`;
}

function timeCard(runs, prompts) {
  return box("time", "Where the time goes <span class=\"dim\">&nbsp;(median per prompt)</span>", timeBreakdown(runs, prompts));
}

// Collapsible box: every part of a run / comparison is one (capabilities, key figures, scaling, time, tables ...).
// Defaults: everything above "Where the time goes" is open, from there on closed (its sub boxes open together with
// it). What the user opens or closes is remembered per page ("run" / "cmp") while the page stays on the same
// selection - switching tabs keeps it, a new run / a new compare selection resets it; sorting and column toggles
// re-draw the page and keep it. title is HTML; sub = a box inside a box. A printed report shows everything.
state.boxOpen = { run: {}, cmp: {} };
let boxPage = "run";  // which page is being drawn (set by renderRun / compareHtml)
const closedByDefault = (key) => key === "time" || key === "checks" || key === "caps" || /^(run|cmp)-/.test(key);
function box(key, title, html, { sub = false, cls = "", closed = false } = {}) {
  const st = state.boxOpen[boxPage] || {};
  const open = state.report || (key in st ? st[key] : !(closed || closedByDefault(key)));
  return `<details class="box${sub ? " sub" : ""}${cls ? " " + cls : ""}" data-key="${key}"${open ? " open" : ""}>` +
    `<summary>${title}</summary><div class="box-body">${html}</div></details>`;
}
const section = (key, title, html) => box(key, esc(title), html);
function rememberSections(root) {
  const page = root.id === "cmp-out" ? "cmp" : "run";
  root.querySelectorAll("details[data-key]").forEach((d) =>
    d.addEventListener("toggle", () => { state.boxOpen[page][d.dataset.key] = d.open; }));
}

// value of run n with its deviation from the baseline (run 0)
function vd(vals, n, digits, higherBetter, unit = "") {
  return n ? `${fmt(vals[n], digits, unit)} <small>${delta(vals[0], vals[n], higherBetter)}</small>` : fmt(vals[0], digits, unit);
}

