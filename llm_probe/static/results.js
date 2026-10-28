"use strict";
// Results - part of the page, loaded in the order of the <script> tags in index.html
// ------------------------------------------------------------------ results
// sort order of the results list: click a column header, click again to reverse
state.runSort = (() => { try { return JSON.parse(recall("run-sort")) || { key: "id", desc: true }; } catch (e) { return { key: "id", desc: true }; } })();
// generic column sort used by the results and prompts lists; accessors[key] maps a row to its sort value
// (a string sorts case-insensitively, a number numerically); null/missing values always go last
function sortBy(items, sort, accessors = {}) {
  if (!sort || !sort.key) return items;
  const { key, desc } = sort;
  const raw = (r) => (accessors[key] ? accessors[key](r) : r[key]);
  const val = (r) => { const v = raw(r); return typeof v === "string" ? v.toLowerCase() || null : v ?? null; };
  const present = items.filter((r) => val(r) !== null), empty = items.filter((r) => val(r) === null);
  present.sort((x, y) => { const a = val(x), b = val(y); return (a < b ? -1 : a > b ? 1 : 0) * (desc ? -1 : 1); });
  return [...present, ...empty];
}
// version strings compare by their numeric parts ("0.10.0" after "0.4.0"), so pad each part for a plain string sort
// capability sort: yes before no before "not tested"; version by its numeric parts (so "0.10" sorts after "0.4")
const CAP_RANK = { yes: 2, no: 1 };
const RUN_SORT_ACC = {
  version: (r) => (r.version ? r.version.split(".").map((n) => String(n).padStart(6, "0")).join(".") : null),
  promptset: (r) => (r.promptset ? r.promptset.split(".").map((n) => String(n).padStart(6, "0")).join(".") : null),
  can_vision: (r) => CAP_RANK[(r.capable || {}).vision] ?? null,
  can_tools: (r) => CAP_RANK[(r.capable || {}).tools] ?? null,
  p_max: (r) => (r.settings || {}).max_tokens ?? null,
  p_temp: (r) => (r.settings || {}).temperature ?? null,
  p_effort: (r) => (r.settings || {}).thinking || "default",
  p_think: (r) => CAP_RANK[r.thinking_capable] ?? null,
  runtime: (r) => runDuration(r),
  quant: (r) => ((r.settings || {}).model_info || {}).quant || null,
  p_timeout: (r) => (r.settings || {}).request_timeout ?? null,
  p_reps: (r) => (r.settings || {}).repetitions ?? null,
  p_conc: (r) => { const lv = (r.settings || {}).concurrency_levels; return Array.isArray(lv) && lv.length ? Math.max(...lv) : null; },
  p_reqslot: (r) => (r.settings || {}).requests_per_level ?? null,
};
function sortRuns(runs) { return sortBy(runs, state.runSort, RUN_SORT_ACC); }
// turn a table's data-sort headers into clickable sort controls that re-render via onSort
function bindSort(table, getSort, setSort, onSort) {
  table.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.add("sortable");
    th.addEventListener("click", () => {
      setSort(nextSort(getSort(), th.dataset.sort));
      onSort();
    });
  });
}
bindSort($("runs-table"), () => state.runSort, (s) => { state.runSort = s; store("run-sort", JSON.stringify(s)); }, loadRuns);

// Refresh when runs changed elsewhere (command line, another browser) or while a run is running: every 10 s the server's
// runs version is asked (it changes with every stored result of a running run). The list always follows; the OPEN VIEW
// (run details, Compare, Responses) is re-fetched only if it shows a run that is running now or was running at the last
// check (so there is one final refresh when it finishes) - finished runs never change. A view in another tab is marked
// stale and refreshed when you open it. Paused while the page is hidden or while you are reading something that a
// re-draw would reset (selected text, an opened "show answers").
const RUNS_REFRESH_MS = 10000;
state.liveIds = new Set(); state.staleViews = new Set();
async function checkRuns() {
  if (document.hidden || Date.now() - (state.runsChecked || 0) < RUNS_REFRESH_MS - 500) return;
  state.runsChecked = Date.now();
  try {
    const v = (await api("/api/runs/version")).version;
    const changed = state.runsVersion !== undefined && v !== state.runsVersion;
    state.runsVersion = v;
    if (changed) await refreshFromPoll();
  } catch (e) { /* server not reachable: pollBench says so */ }
}
document.addEventListener("visibilitychange", () => { syncOutStream(); if (!document.hidden) { state.runsChecked = 0; checkRuns(); } });
// something on the page that a re-draw would reset: selected text or an opened "show answers" block
const readingNow = () => !!(window.getSelection && String(window.getSelection()).trim()) ||
  !!document.querySelector(".cap-answers:not([hidden])");
async function refreshFromPoll() {
  const before = state.liveIds;
  await loadRuns({ views: false });
  const now = new Set(state.runs.filter((r) => r.status === "running").map((r) => r.id));
  state.liveIds = now;
  const touched = new Set([...before, ...now]), known = new Set(state.runs.map((r) => r.id));
  const shows = { runs: state.selectedRun != null ? [state.selectedRun] : [], cmp: state.cmpRuns, ans: state.ansRuns };
  for (const [tab, ids] of Object.entries(shows)) {
    if (ids.some((id) => touched.has(id)) || (tab === "cmp" && ids.some((id) => !known.has(id)))) state.staleViews.add(tab);
  }
  await refreshStale();
}
// re-fetch the stale view of the visible tab (the others when their tab is opened, see showTab)
async function refreshStale() {
  const tab = currentTab;
  if (!state.staleViews.has(tab) || readingNow()) return;
  state.staleViews.delete(tab);
  const y = window.scrollY;
  try {
    if (tab === "runs" && state.selectedRun != null && state.runs.some((r) => r.id === state.selectedRun)) {
      const d = await api("/api/runs/" + state.selectedRun);
      state.runD = d;  // same run: the table sorts and open boxes stay (state.runTbl, state.boxOpen)
      if (document.activeElement !== $("r-note")) $("r-note").value = d.run.note || "";
      drawRun(true);
    } else if (tab === "cmp") await renderCompareNow();
    else if (tab === "ans") await renderResponsesNow();
  } catch (e) { /* the next poll tries again */ }
  window.scrollTo(0, y);
}

// green/yellow/empty indicator for what a run contains (storage.run_contents)
const CONTENT_BOX = { ok: ["ok", "\u2713", "has valid results, run finished cleanly"],
                      partial: ["unk", "!", "aborted, errors or cut-off responses"],
                      none: ["none", "", "not part of this run"] };
function contentBox(contents, set) {
  const [cls, ch, why] = CONTENT_BOX[(contents && contents[set]) || "none"] || CONTENT_BOX.none;
  return `<span class="cbox ${cls}" title="${esc(set + ": " + why)}">${ch}</span>`;
}
// Capability marks of the results list. The shade says how sure it is: a solid mark = the run's own results show it, a hollow
// mark = only the capability check says so (claimed or probed, nothing measured in this run).
// yes = green check, no = cross, null = not tested in this run
function capMark(v, what, evidence) {
  const tested = evidence === "tested", how = tested ? "and this run tested it" : "capability check only - this run did not test it";
  if (v === "yes") return `<span class="cbox ${tested ? "ok" : "hollow"}" title="${esc(`${what}: the model can do it (${how})`)}">✓</span>`;
  if (v === "no") return `<span class="xmark${tested ? " solid" : ""}" title="${esc(`${what}: not available (${tested ? "this run tested it" : "capability check only - this run did not test it"})`)}">✗</span>`;
  return `<span class="dim" title="${esc(what + ": not tested in this run")}">–</span>`;
}
// reasoning ability for the results list (a column of the test parameters): yes / no / not known
function thinkMark(v, evidence) {
  const tested = evidence === "tested";
  const why = "Thinking capable: the capability check found that the model reasons by default or that the effort levels change its reasoning, or this run's answers contained reasoning";
  if (v === "yes") return `<span class="cbox ${tested ? "ok" : "hollow"}" title="${esc(why + "\n\n" + (tested ? "This run's answers showed reasoning." : "Capability check only: no reasoning was seen in this run's answers."))}">✓</span>`;
  if (v === "no") return `<span class="xmark${tested ? " solid" : ""}" title="${esc("No reasoning seen" + (tested ? ": no answer of this run contained reasoning (none was requested)." : ": the capability check says it does not reason by default."))}">✗</span>`;
  return `<span class="dim" title="Not known: this run neither has a capability check nor answers that show it">–</span>`;
}
// how current a run is: grey = the prompt set differs (the wording the model was asked with: results not directly comparable),
// light grey = only the program version differs, normal = both are the current ones
function runStaleness(r) {
  const cur = state.selection || {}, out = { cls: "", version: "", promptset: "" };
  if (!cur.version) return out;
  if (!r.promptset || r.promptset !== cur.promptset) {
    out.cls = "old2";
    out.promptset = r.promptset ? `Prompt set v${r.promptset}, current v${cur.promptset}: the models were asked other wording, the results are not directly comparable.` : "This run did not store its prompt set (made with an old version).";
  } else if (!r.version || r.version !== cur.version) {
    out.cls = "old1";
    out.version = r.version ? `Program version v${r.version}, current v${cur.version}; the prompt set is the current one.` : "This run did not store its program version.";
  }
  return out;
}
// quantization of the tested model; in italics when it was only read from the model name
function quantCell(info) {
  const SRC = { declared: "stated by the server", "command line": "from the server's command line", guessed: "guessed from the model name" };
  if (!info || !info.quant) return `<td class="l dim"${tipAttr("not known (run made before it was recorded, or the server does not say)")}>-</td>`;
  return `<td class="l${info.quant_source === "guessed" ? " guess" : ""}"${tipAttr(`${info.quant} - ${SRC[info.quant_source] || ""}${info.size ? " · size " + billions(info.size) : ""}`)}>${esc(info.quant)}</td>`;
}
// concurrency levels of a run, highest first (e.g. [1,2,4,8] -> "8,4,2,1"); "-" if the load phase had none
function concLevels(s) {
  const lv = s.concurrency_levels;
  return Array.isArray(lv) && lv.length ? [...lv].sort((a, b) => b - a).join(",") : "-";
}
async function loadRuns({ views = true } = {}) {
  try { state.runsVersion = (await api("/api/runs/version")).version; } catch (e) { /* listed below anyway */ }
  state.runs = sortRuns(await api("/api/runs"));
  document.querySelectorAll("#runs-table th[data-sort]").forEach((th) => {
    th.dataset.dir = th.dataset.sort === state.runSort.key ? (state.runSort.desc ? "desc" : "asc") : "";
  });
  const tb = $("runs-table").querySelector("tbody");
  tb.innerHTML = state.runs.map((r) => {
    const t = runTips(r), s = r.settings || {};
    const stale = runStaleness(r);
    return `<tr class="click${r.id === state.selectedRun ? " sel" : ""}${stale.cls ? " " + stale.cls : ""}" data-id="${r.id}">` +
    `<td class="mark"${tipAttr(TIPS.mark)}><input type="checkbox" value="${r.id}"${state.marked.has(r.id) ? " checked" : ""}></td>` +
    `<td${tipAttr(t.id)}>${r.id}</td>` +
    `<td class="l"${tipAttr(t.start)}>${esc(r.started.slice(0, 16).replace("T", " "))}</td>` +
    `<td class="l"${tipAttr(r.base_url)}>${esc(r.endpoint)}</td>` +
    `<td class="l"${tipAttr(KIND_TIPS[r.kind])}>${esc(r.kind)}</td><td class="l"${tipAttr(t.model)}>${esc(r.model)}</td>` +
    quantCell(s.model_info) +
    `<td class="l${statusClass(r) ? " " + statusClass(r) : ""}"${tipAttr(statusTip(r) + (s.error ? "\n\nWhy it failed: " + s.error : ""))}>${r.status === "running" ? liveDot(r) : ""}${esc(r.status)}</td>` +
    `<td${tipAttr(t.runtime)}>${runtimeLabel(r)}</td>` +
    `<td class="l${r.placement_warn ? " yellow" : ""}"${tipAttr(t.placement)}>${esc(r.placement)}</td>` +
    `<td class="l${r.thinking_warn ? " yellow" : ""}"${tipAttr(t.thinking)}>${esc(r.thinking)}</td>` +
    `<td${tipAttr(t.tps)}>${fmt(r.avg_tps)}</td><td${tipAttr(t.ttft)}>${fmt(r.avg_ttft, 2, " s")}</td>` +
    `<td${tipAttr(t.ttfa)}>${fmt(r.avg_ttfa, 2, " s")}</td><td${tipAttr(t.peak)}>${fmt(r.peak, 1, " tok/s")}</td>` +
    `<td${tipAttr(t.check)}>${r.capabilities ? esc(r.capabilities) : r.check_score == null ? "-" : fmt(r.check_score * 100, 0, " %")}</td>` +
    `<td>${contentBox(r.contents, "capabilities")}</td><td>${contentBox(r.contents, "performance")}</td><td>${contentBox(r.contents, "quality")}</td>` +
    `<td>${capMark((r.capable || {}).vision, "Vision", (r.capable_evidence || {}).vision)}</td><td>${capMark((r.capable || {}).tools, "Tool calling", (r.capable_evidence || {}).tools)}</td>` +
    `<td class="l dim"${tipAttr(stale.version)}>${r.version ? "v" + esc(r.version) : "-"}</td>` +
    `<td class="l dim"${tipAttr(stale.promptset)}>${r.promptset ? "v" + esc(r.promptset) : "-"}</td>` +
    `<td class="param-col">${s.max_tokens == null ? "-" : fmt(s.max_tokens, 0)}</td>` +
    `<td class="param-col">${s.temperature == null ? "-" : esc(s.temperature)}</td>` +
    `<td class="l param-col">${esc(s.thinking || "default")}</td>` +
    `<td class="param-col">${thinkMark(r.thinking_capable, (r.capable_evidence || {}).thinking)}</td>` +
    `<td class="param-col">${s.request_timeout == null ? "-" : fmt(s.request_timeout, 0, " s")}</td>` +
    `<td class="param-col">${s.repetitions == null ? "-" : s.repetitions}</td>` +
    `<td class="l param-col${s.run_concurrency ? "" : " dim"}"${tipAttr(s.run_concurrency ? "" : "The concurrent-load phase did not run in this test.")}>${concLevels(s)}</td>` +
    `<td class="param-col">${s.requests_per_level == null ? "-" : s.requests_per_level}</td>` +
    `<td class="l"${tipAttr(r.note)}>${esc(r.note)}</td></tr>`;
  }).join("") || '<tr><td colspan="32" class="dim">No runs yet.</td></tr>';
  tb.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => selectRun(+tr.dataset.id)));
  state.marked = new Set(state.runs.map((r) => r.id).filter((id) => state.marked.has(id)));
  const known = new Set(state.runs.map((r) => r.id));
  if (state.ansRuns.some((id) => !known.has(id))) {  // a run shown in Responses was deleted
    state.ansRuns = state.ansRuns.filter((id) => known.has(id));
    store("ans-runs", state.ansRuns.join(","));
    renderResponses();
  }
  tb.querySelectorAll("td.mark").forEach((td) => td.addEventListener("click", (ev) => {
    ev.stopPropagation();  // marking does not open the run
    const box = td.querySelector("input");
    if (ev.target !== box) box.checked = !box.checked;
    if (box.checked) state.marked.add(+box.value); else state.marked.delete(+box.value);
    updateMarks();
  }));
  updateMarks();
  renderPicker();
  renderAnsPicker();
  if (views) renderCompare();
  if (state.testInfoVersion !== state.runsVersion) { state.testInfoVersion = state.runsVersion; refreshTestInfo(); }  // the Test tab's run-derived info
}

const KIND_TIPS = {
  ollama: "Ollama, queried via its OpenAI-compatible API (native /api/chat when a context length is set).",
  vllm: "vLLM, queried via its OpenAI-compatible API.",
  openwebui: "Open WebUI in front of Ollama - the times include Open WebUI's own overhead.",
  openai: "Any other OpenAI-compatible server.",
};
const STATUS_TIPS = {
  done: "Finished.", running: "Still in progress.", aborted: "Stopped with Abort - results are incomplete.",
  interrupted: "Cut off: the program or the computer was stopped while the test ran. What came in before is kept and still valid; the run is not finished.",
  failed: "Failed, e.g. the warmup did not work (model not loadable, embedding model, server down) or every request failed; see the log of the test.",
};
// the blinking dot of a running run: green; yellow once a request of it failed or hit the time limit (the rest still counts)
const liveTip = (r) => "Running - this page follows it every 10 s." + (r.problems ? `\n\nYellow: ${r.problems} request(s) failed or hit the time limit so far; the results that came in are still valid.` : "");
const liveDot = (r) => `<span class="live-dot${r.problems ? " problem" : ""}"${tipAttr(liveTip(r))}></span>`;
// Message colours (the same everywhere): an abort or failure is yellow if the run already has results to use (what came in stays
// valid), red if nothing usable came in - it is a break then.
function statusClass(r) {
  return r.status === "damaged" ? "red" : r.status === "aborted" || r.status === "failed" || r.status === "interrupted" ? (r.usable ? "yellow" : "red") : "";
}
function statusTip(r) {
  const base = STATUS_TIPS[r.status] || "";
  if (r.status !== "aborted" && r.status !== "failed" && r.status !== "interrupted") return base;
  return base + (r.usable ? "\n\nYellow: results came in before it stopped - they are still valid." : "\n\nRed: nothing usable came in.");
}

// explanations of the values of one row of the results list
function runTips(r) {
  const s = r.settings || {}, t = {};
  const phases = [s.run_single && `single (${s.repetitions} repetitions)`,
    s.run_concurrency && `load (levels ${(s.concurrency_levels || []).join(",")} x ${s.requests_per_level} per slot)`]
    .filter(Boolean).join(" + ");
  t.id = `Run #${r.id}: ${s.prompts} prompts · ${phases || "-"}\nmax_tokens ${s.max_tokens} · temperature ${s.temperature}` +
    ` · thinking ${s.thinking || "model default"}`;
  const dur = r.finished ? Math.round((new Date(r.finished) - new Date(r.started)) / 1000) : null;
  t.start = `Started ${r.started.replace("T", " ")}` + (r.finished ? `, finished ${r.finished.replace("T", " ")}` +
    (dur != null ? ` (${Math.floor(dur / 60)} min ${dur % 60} s)` : "") : "") +
    (s.warmup_s != null ? `\nWarmup/model load: ${s.warmup_s} s` : "");
  t.runtime = r.status === "running" ? "Still running: the runtime is known when it ends." : r.finished
    ? `Runtime ${fmtDuration(runDuration(r))}: ${r.started.replace("T", " ")} to ${r.finished.replace("T", " ")} (all phases, warmup included)`
    : "No end time was stored (the run was interrupted).";
  t.model = r.model + (r.gpu_info ? `\nGPUs: ${r.gpu_info}` : "");
  t.placement = `Placement: ${r.placement}` + (r.placement_warn ? " - part of the model runs on the CPU, which slows generation down" : "") +
    `\nServer parameters: ${r.placement_detail}\nContext: ${r.context}\n\n${COL.Placement}`;
  t.thinking = r.thinking === "-" ? "Unknown: the run has no stored responses (single phase off or failed)."
    : `${r.thinking_detail}` + (r.thinking_warn ? "\nReasoning was switched off, but this model reasons anyway (it cannot be switched off)." : "") +
      `\n\n${COL.Thinking}`;
  t.tps = r.avg_tps == null ? "No single requests." : `Average decode speed of the single requests: ${fmt(r.avg_tps)} tokens/s.\n\n${COL["Avg tok/s"]}`;
  t.ttft = r.avg_ttft == null ? "No single requests."
    : `On average the first token (reasoning included) arrived after ${fmt(r.avg_ttft, 2)} s.\n\n${COL["Avg TTFT"]}`;
  t.ttfa = r.avg_ttfa == null ? (r.avg_ttft == null ? "No single requests." : "No response got to an answer (only reasoning - raise max_tokens).")
    : `On average the answer itself started after ${fmt(r.avg_ttfa, 2)} s` +
      (r.avg_ttft != null && r.avg_ttfa - r.avg_ttft >= 0.05 ? ` - ${fmt(r.avg_ttfa - r.avg_ttft, 2)} s of reasoning before it.` : ".") +
      `\n\n${COL["Avg TTFA"]}`;
  t.peak = r.peak == null ? "No concurrent load phase." : `Best total throughput of the load phase: ${fmt(r.peak, 1)} tokens/s.\n\n${COL["Max throughput"]}`;
  if (r.mode === "capabilities") {
    t.id = `Run #${r.id}: capability check (no speed measurement) · thinking ${s.thinking || "model default"}`;
    t.check = `Capability check: ${r.capabilities} (findings in the run; informational ones not counted).`;
    return t;
  }
  t.check = r.check_score == null ? "No prompt with an expected answer in this run."
    : `On average ${fmt(r.check_score * 100, 0)} % of the expected answer lines were correct (details in the run).\n\n${COL.Check}`;
  return t;
}

const selectRun = (id) => busy(() => selectRunNow(id));
async function selectRunNow(id) {
  if (state.selectedRun !== id) state.boxOpen.run = {};  // a new run: boxes back to their defaults
  state.selectedRun = id;
  $("runs-table").querySelectorAll("tr[data-id]").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.id === id));
  const d = await api("/api/runs/" + id);
  $("r-note").value = d.run.note || "";
  $("r-pdf").disabled = false;
  updateShowButton();
  state.runD = d;
  state.runTbl = {};  // other run: other rows
  drawRun(false);
}
// results tables: the row sort of each table (a click on a header), not kept across runs
state.runTbl = {};
function drawRun(keepScroll = true) {
  const d = state.runD, y = window.scrollY;
  $("run-detail").innerHTML = renderRun(d);
  rememberSections($("run-detail"));
  bindScaling("scaling-run", [{ i: 0, L: "", name: `run #${d.run.id}`, d }]);
  if (keepScroll) window.scrollTo(0, y);
}
$("run-detail").addEventListener("click", (e) => { if (state.runD) sortClick(e, state.runTbl, drawRun); });

function resourceTable(caption, stats) {
  const rows = stats.map((r) => {  // the balance note (HTML) sits next to the GPU figures it qualifies
    const c = [r.phase, fmt(r.gpu_util_avg, 0), fmt(r.gpu_util_max, 0),
      r.vram_max_mb == null ? "-" : `${(r.vram_max_mb / 1024).toFixed(1)}/${((r.vram_total_mb || 0) / 1024).toFixed(0)} GB`,
      fmt(r.power_avg, 0), fmt(r.power_max, 0), fmt(r.temp_max, 0), fmt(r.cpu_avg, 0),
      r.mem_max_mb ? fmt(r.mem_max_mb / 1024, 1, " GB") : "-"].map(esc);
    c.splice(3, 0, gpuBalance(r));
    return c;
  });
  if (!rows.length) rows.push(['<span class="dim">no data</span>', "", "", "", "", "", "", "", "", ""]);
  return table(caption, ["Phase", "Avg GPU %", "Max GPU %", "GPU balance", "Max VRAM", "Avg W", "Max W", "Max °C", "Avg CPU %", "Max RAM"], rows);
}
// "imbalanced" (yellow): several GPUs, but some sat idle while another one worked - a note only, no other figure uses it
function gpuBalance(r) {
  const each = r.gpu_util_each || [];
  if (each.length < 2) return '<span class="dim">-</span>';
  const list = each.map((v) => fmt(v, 0, " %")).join(" / ");
  if (!r.imbalanced) return `<span class="dim" title="${esc(`Average utilization per GPU: ${list}`)}">balanced</span>`;
  const idle = each.filter((v) => v < 5).length;
  return `<span class="yellow" title="${esc(`Only ${each.length - idle} of ${each.length} GPUs did the work (average utilization per GPU: ${list}) - the model probably fits on one GPU, or the server does not split it. The other figures are not affected.`)}">imbalanced</span>`;
}

const CAP_STATUS = { yes: "green", partial: "yellow", no: "red", error: "red", info: "dim" };
const CAP_TIPS = {
  yes: "Works.", partial: "Works only in part - see the detail.", no: "Not supported or ignored.",
  error: "The test request failed for another reason (see the detail).", info: "For information, not a test.",
};
function capCell(f) {
  return f ? `<span class="${CAP_STATUS[f.status] || ""}" title="${esc(CAP_TIPS[f.status] || "")}">${esc(f.status)}</span>` : "-";
}
// what a probe asked and what the model answered (a run without the stored answers only has the one-line detail)
function capAnswers(f) {
  if (!f || !(f.answers || []).length) return "";
  const body = f.answers.map((a) => `<div class="cap-q"><b>Asked:</b> ${esc(a.prompt)}${a.sent ? ` <span class="dim">(${esc(a.sent)})</span>` : ""}</div>` +
    `<pre>${esc(a.answer || "(empty)")}${a.tool_calls ? "\n\n" + "tool calls: " + esc(JSON.stringify(a.tool_calls)) : ""}</pre>`).join("");
  return `<div class="cap-answers" hidden>${body}</div>`;  // collapsed, also in the printed report
}
// ONE button at the right edge of a row: opens (or closes) the answers of every finding in that row at once, each under
// its own result - so the answers of several runs can be read side by side. The printed report keeps them collapsed.
function toggleCapAnswers(btn) {
  const tr = btn.closest("tr"), divs = tr.querySelectorAll(".cap-answers"), open = divs[0].hidden;
  divs.forEach((d) => { d.hidden = !open; });
  btn.textContent = open ? "hide answers" : "show answers";
}
function capAnswersButton(findings) {
  return findings.some((f) => f && (f.answers || []).length) && !state.report
    ? `<button class="btn small no-print" onclick="toggleCapAnswers(this)" title="What the probe asked and what the model answered (every run of this row at once)">show answers</button>` : "";
}
function capabilityTable(rows, runs) {
  // rows: [{group, name, cells: [finding per run]}]; one run: with details, several: details as tooltips
  const one = runs.length === 1;
  const head = ["", ...(one ? ["Result", "Detail"] : runs.map((r) => r.name)), ""];  // the last column holds the answers button
  return ["capability", "setting"].map((g) => `<div class="cap-tbl${one ? " one" : ""}">` + table(g === "capability" ? "Capabilities" : "Settings the endpoint obeys", head,
    rows.filter((x) => x.group === g).map((x) => [esc(x.name), ...(one
      ? [capCell(x.cells[0]), `<span class="cap-detail">${esc(x.cells[0] ? x.cells[0].detail : "")}</span>${capAnswers(x.cells[0])}`]
      : x.cells.map((f) => !f ? "-" : f.status === "info" ? `<span class="cap-detail dim">${esc(f.detail)}</span>`
        : `<span title="${esc(f.detail)}">${capCell(f)}</span>${capAnswers(f)}`)), capAnswersButton(x.cells)])) + "</div>").join("");
}
function hasMeasurements(d) { return d.single.length || d.load.length || Object.keys(d.sessions || {}).length; }
function capabilitySection(d) {
  const rows = (d.run.settings.report || []).map((f) => ({ group: f.group, name: f.name, cells: [f] }));
  if (!rows.length) return "";
  return box("caps", "Capabilities", `<p class="hint">What the model can do and which request settings ` +
    `the endpoint obeys. Hover a result for its detail.</p>${capabilityTable(rows, [{ name: "" }])}`);
}

function renderRun(d) {
  boxPage = "run";
  const r = d.run, s = r.settings;
  const m = (key, text) => `<span class="tip"${tipAttr(COL["meta:" + key])}>${text}</span>`;
  const meas = hasMeasurements(d);
  const ver = s.version ? "LLM Probe v" + esc(s.version) : "LLM Probe version not recorded";
  let out = `<div class="meta"><b>Run #${r.id}</b>${r.status === "running" ? ` ${liveDot(r)}<span class="${r.problems ? "yellow" : "green"}"${tipAttr(liveTip(r))}>live</span>` : ""} ${esc(r.endpoint)} (${esc(r.kind)}, ${esc(r.base_url)}) · model <b>${esc(r.model)}</b> · ${m("version", ver)} · ${m("promptset", s.promptset ? "prompt set v" + esc(s.promptset) : "prompt set not recorded")} · ${m("runtime", "runtime " + runtimeLabel(r))}<br>` +
    `${m("GPU", "GPU: " + esc(r.gpu_info || "-"))} · ${m("max_tokens", "max_tokens " + esc(s.max_tokens) + (s.clamped ? ` (cut to ${esc(s.clamped.min_sent)} where the context of ${esc(s.clamped.context)} needed it)` : ""))} · ` +
    `${m("Temp", "Temp " + esc(s.temperature))} · ${m("thinking", "thinking " + esc(r.thinking_label))} · ${m("warmup", `warmup/load ${esc(s.warmup_s ?? "-")} s`)}` +
    (s.code_word ? ` · <span title="This run offered a random code word instead of 'unsolvable' for 'I cannot answer'; the checks look for it">escape word <code>${esc(s.code_word)}</code></span>` : "") +
    (meas ? ` · ${m("prompts", "prompts: " + esc(groupCounts(d) || "-"))}` : "") + "<br>" +
    r.server_lines.map((l) => `<span class="tip"${tipAttr(COL["line:" + l.name])}>${esc(l.name)}</span>: ` +
      `<span class="${l.warn ? "yellow" : ""}">${l.name === "Server parameters" ? paramSpans(l.text) : esc(l.text)}</span>`).join("<br>") +
    (s.error ? `<br><span class="${statusClass(r) || "red"}"><b>Why this run ${r.status === "aborted" || r.status === "interrupted" ? "stopped" : "failed"}:</b> ${esc(s.error)}</span>` : "") + "</div>";
  out += capabilitySection(d);
  if (!meas) return out + ((s.report || []).length ? "" : '<p class="dim">Nothing recorded for this run.</p>');
  const runs = [{ i: 0, L: "", name: `run #${r.id}`, d }];
  out += kpiCards([...runKpis(d), ...qualityKpis(runs)], runs);
  out += chartsRow("scaling-run", runs, d.single.map((p) => p.prompt_id));
  out += checksCard(d);
  const bySort = (id, items, valueOf) => {
    const st = state.runTbl[id];
    return st ? sortBy(items, st, { [st.key]: valueOf }) : items;
  };
  const sk = (id, keys) => ({ id, keys, cur: state.runTbl[id] });
  out += section("run-single", "Single requests (median)", '<div class="box-opts no-print"><span>click a column header to sort the rows (again: reverse, again: original order)</span></div>' + table("",
    ["Prompt", "TTFT", "TTFA", "Decode tok/s", "E2E tok/s", "Duration", "Prompt tok.", "Output tok.", "Errors", "Cut off", "Check"],
    groupedRows(d.single.map((p) => p.prompt_id), 11, (pids) => bySort("single", pids.map((pid) => d.single.find((p) => p.prompt_id === pid)),
      (p) => { const k = state.runTbl.single.key; return k === "prompt" ? promptName(p.prompt_id) : p[k]; })
      .map((p) => [esc(promptName(p.prompt_id)) + ctxNote(runs, p.prompt_id), fmt(p.ttft, 2, " s"), fmt(p.ttfa, 2, " s"), fmt(p.decode_tps), fmt(p.e2e_tps),
        fmt(p.total, 1, " s"), fmt(p.prompt_tokens, 0), fmt(p.completion_tokens, 0), p.errors || "", cutLabel(p), checkLabel(p)])),
    sk("single", ["prompt", "ttft", "ttfa", "decode_tps", "e2e_tps", "total", "prompt_tokens", "completion_tokens", "errors", null, "check_score"])));
  out += section("run-load", "Concurrent load", '<div class="box-opts no-print"><span>click a column header to sort the rows (again: reverse, again: original order)</span></div>' + table("",
    ["Concurrency", "Requests", "Throughput tok/s", "Req/s", "Avg TTFT", "p95 TTFT", "Avg TTFA", "Avg latency", "p95 latency", "tok/s per req", "Errors"],
    bySort("load", d.load, (l) => l[state.runTbl.load.key]).map((l) => [l.concurrency, l.n, fmt(l.throughput), fmt(l.req_per_s, 2), fmt(l.ttft_avg, 2, " s"),
      fmt(l.ttft_p95, 2, " s"), fmt(l.ttfa_avg, 2, " s"), fmt(l.latency_avg, 1, " s"), fmt(l.latency_p95, 1, " s"), fmt(l.tps_per_req), l.errors || ""]),
    sk("load", ["concurrency", "n", "throughput", "req_per_s", "ttft_avg", "ttft_p95", "ttfa_avg", "latency_avg", "latency_p95", "tps_per_req", "errors"])));
  out += tokenSection("run-tok", [{ i: 0, name: `run #${r.id}`, d }]);
  out += sessionSection("run-sess", [{ i: 0, name: `run #${r.id}`, d }]);
  return out;
}

$("r-as-a").addEventListener("click", () => {
  if (state.selectedRun == null) return;
  setCompare([state.selectedRun, ...state.cmpRuns.filter((id) => id !== state.selectedRun)]);
  showTab("cmp", true);
});
$("r-as-b").addEventListener("click", () => {
  if (state.selectedRun == null) return;
  setCompare([...state.cmpRuns.filter((id) => id !== state.selectedRun), state.selectedRun]);
  showTab("cmp", true);
});
$("r-cmp-marked").addEventListener("click", () => {
  if (state.marked.size < 2) return;
  setCompare([...state.marked].sort((x, y) => x - y));
  showTab("cmp", true);
});
$("r-note-save").addEventListener("click", async () => {
  if (state.selectedRun == null) return;
  await api("/api/runs/" + state.selectedRun, { method: "PATCH", body: { note: $("r-note").value } });
  loadRuns();
});
function updateMarks() {
  const n = state.marked.size, all = $("r-mark-all");
  all.checked = n > 0 && n === state.runs.length;
  all.indeterminate = n > 0 && n < state.runs.length;
  $("r-del").textContent = n ? `Delete marked (${n})` : "Delete";
  $("r-cmp-marked").textContent = n >= 2 ? `Compare marked (${Math.min(n, MAX_RUNS)})` : "Compare marked";
  $("r-cmp-marked").disabled = n < 2;
  const ep = markedEndpoint();  // "Reuse for test" needs the marked runs to share one endpoint
  $("r-reuse").disabled = ep == null || markedParams() === null;  // another endpoint, or the same model with other parameters
  $("r-reuse").textContent = ep ? `Reuse for test (${ep})` : "Reuse for test";
  updateShowButton();
  $("r-export").textContent = n ? `Export marked (${n})` : "Export marked";
  $("r-export").disabled = !n;
}
// "Show responses": the marked runs, or the selected one if none is marked
function responseTargets() {
  return state.marked.size ? [...state.marked].sort((x, y) => x - y).slice(0, MAX_ANS_RUNS)
    : state.selectedRun == null ? [] : [state.selectedRun];
}
function updateShowButton() {
  const n = responseTargets().length;
  $("r-show").disabled = !n;
  $("r-show").textContent = state.marked.size > 1 ? `Show responses (${n})` : "Show responses";
}
$("r-show").addEventListener("click", () => showResponses(responseTargets()));
// "Reuse for test": load the marked runs' endpoint and models into the Test tab (enabled only when all marked
// runs share one endpoint). Missing endpoint -> keep the current one; models gone from the endpoint -> skipped.
function markedRuns() { return state.runs.filter((r) => state.marked.has(r.id)); }
// The parameters the marked runs were made with, per model: {model: {max_tokens, temperature, thinking, num_ctx, request_timeout}}
// (what a run did not store is left out). null = the runs of one model disagree about a parameter (Reuse is greyed out then).
function markedParams() {
  const out = {};
  for (const r of markedRuns()) {
    if (r.mode === "capabilities") continue;  // a capability check has no parameters of its own
    const s = r.settings || {}, mine = {};
    for (const k of ["max_tokens", "temperature", "thinking", "num_ctx", "request_timeout"]) if (s[k] != null) mine[k] = s[k];
    const seen = (out[r.model] = out[r.model] || {});
    for (const [k, v] of Object.entries(mine)) {
      if (k in seen && String(seen[k]) !== String(v)) return null;
      seen[k] = v;
    }
  }
  return out;
}
function markedEndpoint() {
  const eps = [...new Set(markedRuns().map((r) => r.endpoint))];
  return eps.length === 1 ? eps[0] : null;
}
async function reuseMarkedForTest() {
  const ep = markedEndpoint();
  if (ep == null) return;
  const wanted = [...new Set(markedRuns().map((r) => r.model))];
  showTab("test", true);
  const known = (state.cfg || { endpoints: [] }).endpoints.some((e) => e.name === ep);
  if (!known) {
    return setReuseNote([`Endpoint "${ep}" no longer exists - kept the current endpoint and its models.`]);
  }
  $("t-ep").value = ep;
  await loadModels();  // loads the endpoint's current models into #t-models (and clears the note)
  const inputs = [...$("t-models").querySelectorAll("input.mchk")];
  const available = new Set(inputs.map((i) => i.value));
  const missing = wanted.filter((m) => !available.has(m));
  inputs.forEach((i) => { i.checked = wanted.includes(i.value); });
  const carried = applyRunParams(markedParams() || {});
  updateStartLabel();
  if (!inputs.length) return setReuseNote([`Could not load the models of "${ep}" (not reachable?) - nothing selected.`]);
  const picked = wanted.length - missing.length;
  const notes = [`Loaded "${ep}" with ${picked} of ${wanted.length} model(s) selected.`];
  if (missing.length) notes.push(`Skipped (not available on the endpoint): ${missing.join(", ")}.`);
  if (carried) notes.push(`Parameters of the runs set for ${carried} model${carried === 1 ? "" : "s"} (the others: like the Whole test).`);
  setReuseNote(notes);
}
// put the parameters the runs were made with into the model rows; a value equal to the Whole test value needs no setting of its own
function applyRunParams(params) {
  resetModelParams();
  const whole = { max_tokens: fieldValues("t-maxtok"), temperature: fieldValues("t-temp"), num_ctx: fieldValues("t-numctx"), request_timeout: fieldValues("t-timeout") };
  let n = 0;
  for (const row of modelRows()) {
    const p = params[row.dataset.model];
    if (!p) continue;
    let set = false;
    for (const k of ["max_tokens", "temperature", "request_timeout"]) {
      if (p[k] == null || (whole[k].length === 1 && Number(whole[k][0]) === Number(p[k]))) continue;
      row.querySelector(`input[data-p=${k}]`).value = p[k]; set = true;
    }
    if (p.num_ctx != null && isOllama() && !(whole.num_ctx.length === 1 && Number(whole.num_ctx[0]) === Number(p.num_ctx))) {
      row.querySelector("input[data-p=num_ctx]").value = p.num_ctx; set = true;
    }
    if (p.thinking != null && !(thinkValues().length === 1 && thinkValues()[0] === p.thinking)) {  // "" = the model's default
      const box = row.querySelector(`[data-think][value="${CSS.escape(p.thinking)}"]`);
      if (box) { box.checked = true; set = true; }
    }
    if (set) { refreshRow(row); n++; }
  }
  return n;
}
function setReuseNote(notes) {
  const el = $("t-reuse-note");
  if (!el) return;
  el.hidden = !notes.length;
  el.textContent = notes.join(" ");
}
function clearReuseNote() { setReuseNote([]); }
$("r-reuse").addEventListener("click", reuseMarkedForTest);
$("cmp-show").addEventListener("click", () => showResponses(state.cmpRuns));
$("cmp-pdf").addEventListener("click", () => {
  if (state.cmpRuns.length >= 2) window.open(`/?compare=${state.cmpRuns.join(",")}${state.cmpPin != null && state.cmpRuns.includes(state.cmpPin) ? "&pin=" + state.cmpPin : ""}${hostsParam()}`, "_blank");
});
$("r-pdf").addEventListener("click", () => {
  if (state.selectedRun != null) window.open(`/?report=${state.selectedRun}${hostsParam()}`, "_blank");
});
$("r-export").addEventListener("click", async () => {
  const ids = [...state.marked].sort((a, b) => a - b);
  try {
    const res = await fetch("/api/export?runs=" + ids.join(",") + hostsParam());
    if (!res.ok) throw new Error((await res.json()).error || res.statusText);
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "llm-probe-runs.json";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await res.blob()), download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    setMsg($("r-status"), `Exported ${ids.length} run(s) to ${name}.`, "ok");
  } catch (e) { setMsg($("r-status"), "Export failed: " + e.message, "err"); }
});
$("r-import").addEventListener("click", () => $("r-import-file").click());
$("r-import-file").addEventListener("change", async () => {
  const file = $("r-import-file").files[0];
  $("r-import-file").value = "";
  if (!file) return;
  try {
    const res = await fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: await file.text() });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || res.statusText);
    await loadRuns();
    setMsg($("r-status"), `Imported ${d.imported.length} run(s) from ${file.name}` +
      (d.imported.length ? ` (new numbers ${d.imported.map((i) => "#" + i).join(", ")})` : "") +
      (d.skipped ? `; ${d.skipped} already existed and were skipped` : "") + ".", "ok");
  } catch (e) { setMsg($("r-status"), "Import failed: " + e.message, "err"); }
});
// show/hide the test-parameter columns (off by default to keep the table narrow), remembered per browser
(function initParamColumns() {
  const on = recall("show-params") === "1";
  $("r-params").checked = on;
  $("runs-table").classList.toggle("hide-params", !on);
  $("r-params").addEventListener("change", () => {
    store("show-params", $("r-params").checked ? "1" : "0");
    $("runs-table").classList.toggle("hide-params", !$("r-params").checked);
  });
})();
bindHeadCheck($("r-mark-all"), () => [...$("runs-table").querySelectorAll("td.mark input")], () => {
  state.marked = new Set([...$("runs-table").querySelectorAll("td.mark input:checked")].map((i) => +i.value));
  updateMarks();
});
$("r-del").addEventListener("click", async () => {
  const ids = state.marked.size ? [...state.marked].sort((x, y) => y - x)
    : state.selectedRun == null ? [] : [state.selectedRun];
  if (!ids.length || !confirm(ids.length > 1 ? `Delete ${ids.length} runs (${ids.map((i) => "#" + i).join(", ")})?`
    : `Delete run #${ids[0]}?`)) return;
  const failed = [];
  for (const id of ids) {
    try {
      await api("/api/runs/" + id, { method: "DELETE" });
      state.marked.delete(id);
      if (id === state.selectedRun) {
        state.selectedRun = null;
        $("run-detail").innerHTML = '<span class="dim">Select a run.</span>';
        updateShowButton();
      }
    } catch (e) { failed.push(`#${id}: ${e.message}`); }
  }
  loadRuns();
  if (failed.length) alert("Not deleted:\n" + failed.join("\n"));
});

