"use strict";
// Test tab: estimated run time, prompt table, start - part of the page, loaded in the order of the <script> tags in index.html
// ---- estimated run time: built in the browser from the timing profile of the endpoint's earlier runs (/api/models/timing)
state.timing = null;
async function loadTiming(name, quiet = false) {
  if (!quiet) { state.timing = null; updateEstimate(); }
  try {
    const t = await api("/api/models/timing?endpoint=" + encodeURIComponent(name));
    if (state.modelsFor !== name) return;
    state.timing = t;
  } catch (e) { if (!quiet) state.timing = { models: {}, runs: 0, partial: false }; }
  updateEstimate();
}
// the runs changed (a test finished, the command line added one): what the Test tab derives from them - the estimated run time,
// the reasoning and capabilities that earlier runs found - is read again, without asking the endpoint or touching the choices
async function refreshTestInfo() {
  const name = state.modelsFor, rows = modelRows();
  if (!name || !rows.length || $("t-ep").value !== name) return;
  loadTiming(name, true);
  try {
    const r = await api("/api/models/hints?endpoint=" + encodeURIComponent(name) + rows.map((x) => "&model=" + encodeURIComponent(x.dataset.model)).join(""));
    if (state.modelsFor !== name) return;
    state.modelInfo = r.reasoning || {}; state.modelCaps = r.capabilities || {};
    rows.forEach((row) => { refreshRow(row); renderModelCaps(row); });
    if (state.modelSort && ["chat", "vision", "tools"].includes(state.modelSort.key)) sortModels();
    updateStartLabel();
  } catch (e) { /* the next change tries again */ }
}
// seconds of ONE run of `model` with these settings, from its newest earlier run (the one with the same thinking setting first);
// null = no earlier run. The prompts the run did not have get the average of the model's other prompts, marked rough.
function estimateRun(runs, think, prompts, st) {
  if (!runs || !runs.length) return null;
  const ref = runs.find((r) => r.thinking === think) || runs[0];
  const known = (id, phase) => { for (const r of [ref, ...runs]) if (r[phase] && r[phase][id] != null) return r[phase][id]; return null; };
  let sec = ref.overhead, rough = ref.thinking !== think;
  const single = Object.values(ref.single), avg = single.length ? single.reduce((a, b) => a + b, 0) / single.length : 0;
  for (const p of prompts) {
    const phase = p.kind === "deviation" ? "session" : p.kind === "sweep" ? "sweep" : "single";
    if (phase === "single" && !st.single) continue;
    if (phase === "sweep" && !st.single) continue;
    const t = known(p.id, phase);
    if (t == null) rough = true;
    sec += (t ?? (phase === "single" ? avg : 0)) * (phase === "single" ? st.reps : 1);
  }
  if (st.load) {
    const walls = Object.values(ref.load), mean = walls.length ? walls.reduce((a, b) => a + b, 0) / walls.length : 0, scale = st.rps / (ref.rps || st.rps);
    for (const c of st.levels) sec += (ref.load[c] ?? mean) * scale;
    if (!walls.length && st.levels.length) rough = true;
  }
  return { sec, rough };
}
function updateEstimate() {
  const el = $("t-est");
  if (!el) return;
  const rows = modelRows().filter((r) => r.querySelector(".mchk").checked);
  if (!rows.length) { el.textContent = ""; el.classList.remove("warn"); return; }
  if (!state.timing) { el.textContent = "Estimated run time: calculating ..."; el.classList.remove("warn"); return; }
  const prompts = promptBoxes().filter((i) => i.checked).map((i) => promptById(i.value)).filter(Boolean);
  const st = { reps: Math.max(1, +$("t-reps").value || 1), single: $("t-single").checked, load: $("t-load").checked, levels: fieldValues("t-levels").filter((x) => /^\d+$/.test(x)),
               rps: Math.max(1, +$("t-rpl").value || 1) };
  let total = 0, unknown = 0, rough = false;
  for (const row of rows) {
    const v = effectiveValues(row), n = (a) => Math.max(1, new Set(a).size), ollama = isOllama();
    const perThink = n(v.max_tokens) * n(v.temperature) * n(v.request_timeout) * (ollama ? n(v.num_ctx) : 1);
    let model = 0, any = false;
    for (const think of (v.thinking.length ? v.thinking : [""])) {
      const e = estimateRun(state.timing.models[row.dataset.model], think, prompts, st);
      if (e) { model += e.sec * perThink; any = true; rough = rough || e.rough; }
    }
    if (any) total += model; else unknown++;
  }
  const based = state.timing.partial ? `based on the newest ${state.timing.runs} runs` : "based on earlier runs";
  const parts = total ? `~${fmtDuration(total)}` : "";
  el.textContent = "Estimated run time: " + (total ? parts + (unknown ? ` + unknown for ${unknown} model${unknown > 1 ? "s" : ""}` : "") + ` (${based}${rough ? ", partly rough" : ""})`
    : `unknown (no earlier run of the selected model${rows.length > 1 ? "s" : ""})`);
  el.classList.toggle("warn", total > 3 * 3600);
  el.title = TIPS["t-est"];
}
state.estimateText = () => ($("t-est") ? $("t-est").textContent : "");
// num_ctx: a setting of Ollama (editable). Where the server reports the context but it cannot be set from here (vLLM: --max-model-len
// when the server starts) the value is shown greyed out; where there is nothing to show and nothing to set, the field is hidden.
const NUMCTX_TIP = "The context the server runs the model with (vLLM: --max-model-len, chosen when the server starts). It cannot be set from here.";
function refreshNumCtx() {
  const ollama = isOllama(), inp = $("t-numctx"), lab = $("t-numctx-label");
  const ctx = (m) => (state.modelDetails[m] || {}).context;
  const shown = modelRows().filter((r) => r.querySelector(".mchk").checked).map((r) => ctx(r.dataset.model)).filter(Boolean);
  const known = Object.values(state.modelDetails).some((d) => d && d.context);
  lab.hidden = !(ollama || known);
  $("t-models").classList.toggle("no-numctx", !(ollama || known));
  inp.disabled = !ollama;
  lab.querySelector(".dim").innerHTML = ollama ? "(Ollama;<br>several = batch)" : "(read from the server)";  // (fixed texts, no data)
  if (ollama) {
    if (inp.dataset.mode === "server") inp.value = state.numctxUser ?? "";  // back from a value read from the server; never while typing
    inp.dataset.mode = "ollama"; inp.placeholder = "empty = the endpoint's setting"; inp.title = "";
  } else {
    inp.dataset.mode = "server";
    const u = [...new Set(shown)];
    inp.value = u.length === 1 ? String(u[0]) : u.length ? `${Math.min(...u)} - ${Math.max(...u)}` : "";
    inp.placeholder = "per model"; inp.title = NUMCTX_TIP;
  }
  modelRows().forEach(refreshRowNumCtx);
}
function refreshRowNumCtx(row) {
  const ollama = isOllama(), c = (state.modelDetails[row.dataset.model] || {}).context, inp = row.querySelector("input[data-p=num_ctx]"), lab = row.querySelector(".mp-numctx");
  if (!inp) return;
  lab.hidden = !(ollama || c);
  inp.disabled = !ollama;
  if (ollama) { inp.title = ""; return; }
  inp.value = c ? String(c) : ""; inp.title = NUMCTX_TIP;
}
function updateStartLabel() {
  const n = checkedModels().length, total = $("t-models").querySelectorAll("input.mchk").length, runs = batchRuns();
  const ollama = isOllama();
  refreshNumCtx();
  $("t-start").textContent = runs > 1 ? (n > 1 && runs === n ? `Start batch (${n} models)` : `Start batch (${runs} runs)`) : "Start test";
  $("tm-count").textContent = total ? `${n} of ${total} selected` + (runs > n ? ` · ${runs} runs` : "") : "";
  $("tm-reset").disabled = !modelRows().some((r) => Object.keys(ownParams(r)).length);
  updateEstimate();
}
["t-maxtok", "t-temp", "t-numctx", "t-timeout", "t-think"].forEach((id) => $(id).addEventListener(id === "t-think" ? "change" : "input", updateStartLabel));
$("t-numctx").addEventListener("input", () => { if (isOllama()) state.numctxUser = $("t-numctx").value; });
["t-reps", "t-levels", "t-rpl", "t-timeout"].forEach((id) => $(id).addEventListener("input", updateEstimate));
["t-single", "t-load"].forEach((id) => $(id).addEventListener("change", updateEstimate));
$("t-models").addEventListener("change", (ev) => {
  if (ev.target.matches("input.mchk") && $("tm-head")) syncHeadCheck($("tm-head"), () => [...$("t-models").querySelectorAll("input.mchk")]);
  updateStartLabel();
});
$("t-ep").addEventListener("change", loadModels);

function fillSettings(b) {
  $("t-maxtok").value = (b.max_tokens_values && b.max_tokens_values.length ? b.max_tokens_values : [b.max_tokens]).join(", ");
  $("t-temp").value = (b.temperature_values && b.temperature_values.length ? b.temperature_values : [b.temperature]).join(", ");
  state.numctxUser = (b.num_ctx_values || []).join(", ");
  $("t-numctx").value = state.numctxUser;
  $("t-reps").value = b.repetitions;
  $("t-levels").value = b.concurrency_levels.join(","); $("t-rpl").value = b.requests_per_level;
  $("t-timeout").value = (b.request_timeout_values && b.request_timeout_values.length ? b.request_timeout_values : [b.request_timeout]).map((x) => Math.round(x)).join(", ");
  $("t-single").checked = b.run_single;
  $("t-load").checked = b.run_concurrency; $("t-escape").value = b.escape_word || "";
  const think = b.thinking_values && b.thinking_values.length ? b.thinking_values : [b.thinking || ""];
  setWholeThink(think);
  syncPhases();
}
// a phase that is switched off greys out and disables its fields (a disabled control still reports its value, so the
// settings are saved either way; the checkbox sits in the legend, which a disabled fieldset leaves alone)
function syncPhases() {
  for (const id of ["t-single", "t-load"]) {
    const box = $(id), group = box.closest("fieldset");
    group.disabled = !box.checked;
    group.classList.toggle("off", !box.checked);
  }
}
["t-single", "t-load"].forEach((id) => $(id).addEventListener("change", syncPhases));
// the values a comma separated field spans (a batch run is models x values of every multi-valued setting)
const fieldValues = (id) => $(id).value.split(/[,;]/).map((x) => x.trim()).filter(Boolean);
// Whole test > Thinking: the same compact drop-down with check boxes as in the model rows (several = batch)
const thinkValues = () => { const v = [...$("t-think").querySelectorAll("input:checked")].map((i) => i.value); return v.length ? v : [""]; };
function setWholeThink(values) {
  $("t-think").querySelectorAll("input").forEach((i) => { i.checked = values.includes(i.value); });
  refreshWholeThink();
}
function refreshWholeThink() {
  const v = thinkValues();
  $("t-think").querySelector("summary").textContent = v.map(thinkName).join(" + ");
}
$("t-think").querySelector(".tlist").innerHTML = THINK_CHOICES.map(([v, t]) => `<label><input type="checkbox" value="${v}">${t}</label>`).join("");
$("t-think").addEventListener("change", refreshWholeThink);
function batchRuns() {  // the runs of every ticked model: its own values or the whole test's, multiplied
  const ollama = isOllama(), n = (a) => Math.max(1, new Set(a).size);
  const runsOf = (v) => n(v.max_tokens) * n(v.temperature) * n(v.thinking) * n(v.request_timeout) * (ollama ? n(v.num_ctx) : 1);
  const rows = modelRows().filter((r) => r.querySelector(".mchk").checked);
  if (!rows.length) return runsOf({ max_tokens: fieldValues("t-maxtok"), temperature: fieldValues("t-temp"), thinking: thinkValues(), num_ctx: fieldValues("t-numctx"), request_timeout: fieldValues("t-timeout") });
  return rows.reduce((sum, r) => sum + runsOf(effectiveValues(r)), 0);
}

function renderPromptImages() {
  $("pr-images").innerHTML = state.promptImages.map((f, n) => `<span class="pr-img"><a href="/api/images/${encodeURIComponent(f)}" ` +
    `target="_blank" title="${esc(f)}"><img src="/api/images/${encodeURIComponent(f)}" alt="${esc(f)}"></a>` +
    `<button data-n="${n}" title="Remove this image from the prompt">x</button></span>`).join("") ||
    '<span class="dim">none</span>';
  $("pr-images").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    state.promptImages.splice(+b.dataset.n, 1); renderPromptImages();
  }));
}
$("pr-img-add").addEventListener("click", () => $("pr-img-file").click());
$("pr-img-file").addEventListener("change", async () => {
  for (const file of $("pr-img-file").files) {
    try {
      const res = await fetch("/api/images?name=" + encodeURIComponent(file.name), { method: "POST", body: file });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
      state.promptImages.push(d.name);
      setMsg($("pr-status"), `Uploaded ${d.name} - save the prompt to use it.`, "ok");
    } catch (e) { setMsg($("pr-status"), `Upload of ${file.name} failed: ${e.message}`, "err"); }
  }
  $("pr-img-file").value = "";
  renderPromptImages();
});
function promptExtras(p, images = true) {
  const parts = images && p.images.length ? [`image${p.images.length > 1 ? "s" : ""} ${p.images.join(", ")}`] : [];
  if (p.tools.length) parts.push(`${p.tools.length} tools (${p.tools.map((t) => t.function.name).join(", ")})`);
  if (p.turns.length) parts.push(`${p.turns.length} further turns (tool call and result)`);
  return parts.join(" · ");
}
function promptPreview(p) {
  const cut = (t, n) => (t.length > n ? t.slice(0, n) + " ..." : t);
  const extra = promptExtras(p);
  return cut(p.text, 400) + (extra ? "\n\nAlso sends: " + extra : "") +
    (p.expected ? "\n\nExpected answer:\n" + cut(p.expected, 200) : "");
}

// the prompt selection of the Test tab: a table, one row per prompt. The filters above it (Group, Language, Level,
// Category, search) only narrow what is shown; the ticks stay where they are. "all" / "none" / the presets tick by rule.
const FACET_TITLES = { lang: "Language", difficulty: "Difficulty", group: "Group", kind: "Type" };  // the facets of the rules (default, presets)
const GROUP_INFO = { performance: "speed and throughput, answers not checked", quality: "answers checked automatically",
                     reliability: "taken from the source, failures announced" };
const PFILTERS = [["group", "Group", (p) => p.group], ["lang", "Language", (p) => p.lang || ""],
                  ["difficulty", "Level", (p) => p.difficulty || "easy"], ["category", "Category", (p) => p.category]];
const promptBoxes = () => [...$("t-prompts").querySelectorAll("input:not([data-caps]):not(#tp-head)")];
const shownBoxes = () => promptBoxes().filter((i) => !i.closest("tr").hidden);
const capSelected = () => !!($("t-caps-sel") && $("t-caps-sel").checked);
const promptById = (id) => state.prompts.find((p) => p.id === id);
const selects = (p, sel) => sel.lang.includes(p.lang || "") && sel.difficulty.includes(p.difficulty || "easy")
  && sel.group.includes(p.group) && sel.kind.includes(p.kind);
// the values of one filter in a sensible order, with their labels and the number of prompts
function filterValues(f) {
  const get = PFILTERS.find((x) => x[0] === f)[2];
  const known = f === "category" ? [] : state.selection.facets[f], label = Object.fromEntries(known), order = known.map(([v]) => v);
  const vals = [...new Set(state.prompts.map(get))].sort((a, b) => ((order.indexOf(a) + 1) || 99) - ((order.indexOf(b) + 1) || 99) || a.localeCompare(b));
  return vals.map((v) => [v, label[v] || v || "no language", state.prompts.filter((p) => get(p) === v).length]);
}
// column sort of the table: ascending -> descending -> the original order (grouped); the ticks stay with their prompts
state.ptSort = null;
const PSORT = { name: (p) => p.name, group: (p) => p.group, lang: (p) => p.lang || "", difficulty: (p) => p.difficulty || "easy", category: (p) => p.category };
const filterBoxes = (f) => [...$("t-facets").querySelectorAll(`input[data-pf=${f}]`)];
function filterSummary(f, title) {
  const all = filterBoxes(f), on = all.filter((b) => b.checked);
  return `${title}: ` + (on.length === all.length ? "all" : on.length ? on.map((b) => b.dataset.label).join(" + ") : "none");
}
function renderFilters() {
  const off = new Set(filterBoxes("group").concat(filterBoxes("lang"), filterBoxes("difficulty"), filterBoxes("category")).filter((b) => !b.checked).map((b) => `${b.dataset.pf}:${b.value}`));
  const was = $("t-psearch") ? $("t-psearch").value : "";
  $("t-facets").innerHTML = PFILTERS.map(([f, title]) =>
    `<details class="tdd" data-pfd="${f}"><summary></summary><div class="tlist"><div class="presets"><a href="#" data-pfset="all" data-f="${f}">all</a> / <a href="#" data-pfset="none" data-f="${f}">none</a></div>` +
    filterValues(f).map(([v, label, n]) => `<label><input type="checkbox" data-pf="${f}" data-label="${esc(label)}" value="${esc(v)}"${off.has(`${f}:${v}`) ? "" : " checked"}>` +
      `${esc(label)} <span class="dim">${n}</span></label>`).join("") + "</div></details>").join("") +
    `<input type="search" id="t-psearch" placeholder="search name ..." value="${esc(was)}">`;
  applyFilters();
}
// hide the rows the filters do not take; the header check box and the counts follow what is shown
function applyFilters() {
  const on = Object.fromEntries(PFILTERS.map(([f]) => [f, new Set(filterBoxes(f).filter((b) => b.checked).map((b) => b.value))]));
  const q = ($("t-psearch") ? $("t-psearch").value : "").trim().toLowerCase();
  promptBoxes().forEach((i) => {
    const p = promptById(i.value);
    i.closest("tr").hidden = !(PFILTERS.every(([f, , get]) => on[f].has(get(p))) &&
      (!q || p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q)));
  });
  PFILTERS.forEach(([f, title]) => { const d = $("t-facets").querySelector(`[data-pfd=${f}] summary`); if (d) d.textContent = filterSummary(f, title); });
  if ($("tp-head")) syncHeadCheck($("tp-head"), shownBoxes);
  updatePromptCounts();
}
// one line under the filter: what the filter shows and what is ticked (the ticks count even when a row is hidden)
function updatePromptCounts() {
  const all = state.prompts.length, ticked = promptBoxes().filter((i) => i.checked).length;
  $("t-selcount").innerHTML = `<b>${shownBoxes().length} of ${all} shown</b>, ${ticked} of ${all} selected` + (capSelected() ? " + capabilities" : "");
}
function updateSelCount() {
  if ($("tp-head")) syncHeadCheck($("tp-head"), shownBoxes);
  if (typeof updateEstimate === "function") updateEstimate();
  updatePromptCounts();
}
// tick the prompts a rule (the default, a preset, all, none) selects; the filters are reset so that the ticks are visible.
// Long sessions, refusal tests and the sweep only after their notice was confirmed.
async function tickByRule(sel) {
  const kinds = {};
  for (const [kind, ask] of Object.entries(CONFIRM_KIND)) {
    kinds[kind] = !state.prompts.some((p) => p.kind === kind && selects(p, sel)) || await ask();
  }
  promptBoxes().forEach((i) => { const p = promptById(i.value); i.checked = selects(p, sel) && kinds[p.kind] !== false; });
  $("t-facets").querySelectorAll("input[data-pf]").forEach((b) => { b.checked = true; });
  $("t-psearch").value = "";
  applyFilters();
  updateSelCount();
}
function renderPromptChecklist() {
  const box = $("t-prompts");
  const first = !box.querySelector("tbody");
  const checked = new Set(promptBoxes().filter((i) => i.checked).map((i) => i.value));
  renderFilters();
  const row = (p) => {
    const on = first ? selects(p, state.selection.default) : checked.has(p.id);
    return `<tr${tipAttr(promptPreview(p))}><td class="pc"><input type="checkbox" value="${esc(p.id)}"${on ? " checked" : ""}></td>` +
      `<td class="pn">${esc(p.name)}` + p.needs.map((n) => `<span class="needs">${esc(n)}</span>`).join("") +
      (p.session.task ? '<span class="needs" title="Executes the code the model writes, in a sandbox">runs code</span>' : "") + "</td>" +
      `<td>${esc(p.group)}</td><td>${esc(p.lang || "-")}</td><td>${esc(p.difficulty || "easy")}</td><td>${esc(p.category)}</td></tr>`;
  };
  const order = [...Object.keys(GROUP_INFO), ...new Set(state.prompts.map((p) => p.group))];
  let sorted = [...new Set(order)].flatMap((g) => state.prompts.filter((p) => p.group === g));
  const so = state.ptSort;
  if (so && so.key) sorted = sorted.map((p, i) => [p, i]).sort(([a, i], [b, j]) => (PSORT[so.key](a).localeCompare(PSORT[so.key](b)) * (so.desc ? -1 : 1)) || i - j).map(([p]) => p);
  const th = (key, title) => `<th class="sort" data-psort="${key}"${so && so.key === key ? ` data-dir="${so.desc ? "desc" : "asc"}"` : ""}${tipAttr(SORT_TIP)}>${title}</th>`;
  box.innerHTML = `<table class="ptable"><colgroup><col style="width:28px"><col><col style="width:92px"><col style="width:46px"><col style="width:54px"><col style="width:100px"></colgroup><thead><tr><th class="pc"><input type="checkbox" id="tp-head"${tipAttr(HEAD_TIP)}></th>${th("name", "Prompt")}${th("group", "Group")}${th("lang", "Lang")}${th("difficulty", "Level")}${th("category", "Category")}</tr></thead><tbody>` +
    `<tr class="caps"${tipAttr(TIPS["t-caps-sel"])}><td class="pc"><input type="checkbox" data-caps id="t-caps-sel" checked disabled></td>` +
    `<td colspan="5" style="white-space:normal">Capability check <span class="dim">(always on - the baseline for fair scoring)</span></td></tr>` +
    sorted.map(row).join("") + "</tbody></table>";
  bindHeadCheck($("tp-head"), shownBoxes, async () => {  // the rotation may tick kinds that ask first (long sessions, refusal tests, sweep)
    for (const [kind, ask] of Object.entries(CONFIRM_KIND)) {
      const mine = promptBoxes().filter((i) => i.checked && promptById(i.value).kind === kind);
      if (mine.length && !(await ask())) mine.forEach((i) => { i.checked = false; });
    }
    updateSelCount();
  });
  applyFilters();
  updateSelCount();
}
// The warning dialogs (long sessions, refusal tests, context sweep) come once per browser session (tab) and kind, so
// they do not nag; only a refusal test against a hosted ("public") endpoint asks again for each such endpoint.
const okOnce = (k) => { try { return sessionStorage.getItem("ok:" + k) === "1"; } catch (e) { return !!state["ok_" + k]; } };
const setOk = (k) => { state["ok_" + k] = true; try { sessionStorage.setItem("ok:" + k, "1"); } catch (e) { /* kept in state */ } };
// long sessions run model-written code: ask once per session before they are ticked
async function confirmSessions() {
  if (okOnce("sessions")) return true;
  let how = "an isolated Python process";
  try { how = (await api("/api/sandbox")).description; } catch (e) { /* keep the generic text */ }
  const ok = confirm("Long sessions (deviation tests)\n\n" +
    "The model develops a small Python module over 12 steps. After every step the code it wrote is executed " +
    "against tests - on the machine LLM Probe runs on, in a sandbox:\n" + how + ".\n\n" +
    "They run last, after all other phases, and take several minutes per model (12 requests with a growing " +
    "context, max_tokens at least 4096).\n\nSelect them?");
  if (ok) setOk("sessions");
  return ok;
}
// refusal tests may be flagged by a hosted service: ask once per session (again for every hosted endpoint)
async function confirmSafety() {
  const ep = $("t-ep").value;
  let text = "Refusal tests (safety)\n\nSome of these requests may be flagged by a hosted service. Select them?", key = "safety";
  try { const n = await api("/api/safety-notice?endpoint=" + encodeURIComponent(ep)); text = n.text; if (n.public) key = "safety:" + ep; } catch (e) { /* generic */ }
  if (okOnce(key)) return true;
  const ok = confirm(text);
  if (ok) setOk(key);
  return ok;
}
// the context sweep is long: ask once per session
async function confirmSweep() {
  if (okOnce("sweep")) return true;
  const ok = confirm("Context sweep - this test takes a long time\n\nThe log triage at growing sizes (2k, 4k, 8k ... up to 85 % of the " +
    "context, at most 64k tokens), every size as often as the repetitions. It runs after the load phase and takes several minutes per " +
    "model - much longer for models with a large context or slow prompt processing.\n\nSelect it?");
  if (ok) setOk("sweep");
  return ok;
}
// kinds of prompts that ask before they are ticked
const CONFIRM_KIND = { deviation: confirmSessions, safety: confirmSafety, sweep: confirmSweep };
$("t-prompts").addEventListener("change", async (ev) => {
  const p = promptById(ev.target.value);
  if (ev.target.checked && p && CONFIRM_KIND[p.kind] && !(await CONFIRM_KIND[p.kind]())) ev.target.checked = false;
  updateSelCount();
});
$("t-prompts").addEventListener("change", (ev) => { if (ev.target.dataset.caps !== undefined) updateSelCount(); });
function setCaps(on) { if ($("t-caps-sel")) $("t-caps-sel").checked = on; updateSelCount(); }
$("t-facets").addEventListener("change", applyFilters);
$("t-facets").addEventListener("click", (ev) => {
  const a = ev.target.closest("a[data-pfset]");
  if (!a) return;
  ev.preventDefault();
  filterBoxes(a.dataset.f).forEach((b) => { b.checked = a.dataset.pfset === "all"; });
  applyFilters();
});
$("t-facets").addEventListener("input", (ev) => { if (ev.target.id === "t-psearch") applyFilters(); });
$("t-prompts").addEventListener("click", (ev) => {
  const h = ev.target.closest("th[data-psort]");
  if (h) { state.ptSort = nextSort(state.ptSort, h.dataset.psort); renderPromptChecklist(); }
});
// a click on the name ticks the row, like on a label
$("t-prompts").addEventListener("click", (ev) => {
  const cell = ev.target.closest("td.pn");
  if (cell) { const b = cell.parentElement.querySelector("input"); b.checked = !b.checked; b.dispatchEvent(new Event("change", { bubbles: true })); }
});
// an open filter list closes on a click elsewhere
document.addEventListener("click", (ev) => {
  document.querySelectorAll("#t-facets details[open]").forEach((d) => { if (!d.contains(ev.target)) d.open = false; });
});
const allFacets = () => Object.fromEntries(Object.keys(FACET_TITLES).map((f) => [f, state.selection.facets[f].map(([v]) => v)]));
$("t-all").addEventListener("click", (ev) => { ev.preventDefault(); tickByRule(allFacets()); setCaps(true); });
$("t-none").addEventListener("click", (ev) => { ev.preventDefault(); tickByRule({ lang: [], difficulty: [], group: [], kind: [] }); setCaps(false); });
// presets: a rule each (English; every difficulty and kind; speed = performance, quality = quality + reliability)
for (const [id, group] of [["t-speed", ["performance"]], ["t-quality", ["quality", "reliability"]]]) {
  $(id).addEventListener("click", (ev) => {
    ev.preventDefault();
    const all = allFacets();
    tickByRule({ ...all, lang: all.lang.filter((v) => v === "en" || v === ""), group });
    setCaps(true);
  });
}
$("t-default").addEventListener("click", (ev) => { ev.preventDefault(); tickByRule(state.selection.default); setCaps(state.selection.capabilities_default); });

// keep a scroll box at its end while text arrives, unless the user scrolled up (also while its tab is hidden)
function follow(box, update) {
  if (box.clientHeight) box.dataset.follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 20 ? "1" : "";
  update();
  if (box.dataset.follow !== "") box.scrollTop = box.scrollHeight;
}

function appendLog(html) {
  follow($("log"), () => $("log").insertAdjacentHTML("beforeend", html + "\n"));
}

// start a test or capability check; if another process (command line, a second server) runs a test, ask whether to start anyway
async function startBench(body) {
  try {
    await api("/api/bench/start", { method: "POST", body });
  } catch (e) {
    if (!(e.data && e.data.locked)) throw e;
    const ok = e.data.overlap === "no" ? "start now" : "start anyway";
    if (!confirm(`${e.message}\n\n${e.data.question}\n\nOK = ${ok}  ·  Cancel = do not start`)) {
      throw new Error(`${e.message} Not started.`);
    }
    await api("/api/bench/start", { method: "POST", body: { ...body, force: true } });
  }
  $("log").innerHTML = "";
  state.benchSeq = 0;
  pollBench();
}

$("t-start").addEventListener("click", async () => {
  const chosen = promptBoxes().filter((i) => i.checked).map((i) => promptById(i.value));
  if (chosen.some((p) => p.kind === "safety") && !(await confirmSafety())) return;  // e.g. another endpoint now
  const body = {
    endpoint: $("t-ep").value, models: checkedModels(), capabilities: capSelected(),
    prompts: promptBoxes().filter((i) => i.checked).map((i) => i.value),
    settings: {
      max_tokens: $("t-maxtok").value, temperature: $("t-temp").value, num_ctx: isOllama() ? $("t-numctx").value : (state.numctxUser || ""), repetitions: $("t-reps").value,
      concurrency_levels: $("t-levels").value, requests_per_level: $("t-rpl").value,
      request_timeout: $("t-timeout").value, run_single: $("t-single").checked, run_concurrency: $("t-load").checked,
      thinking: thinkValues(), escape_word: $("t-escape").value,
    },
    // what the user set per model (remembered) and what the ticked models are run with (their thinking narrowed to what they accept)
    model_params: Object.fromEntries(modelRows().map((r) => [r.dataset.model, ownParams(r)]).filter(([, p]) => Object.keys(p).length)),
    model_effective: Object.fromEntries(modelRows().filter((r) => r.querySelector(".mchk").checked).map((r) => {
      const o = ownParams(r), eff = effectiveThinking(r);
      if (JSON.stringify(eff) !== JSON.stringify(o.thinking || thinkValues())) o.thinking = eff;
      return [r.dataset.model, o];
    }).filter(([, p]) => Object.keys(p).length)),
  };
  const runs = batchRuns();
  if (runs > 12 && !confirm(`This starts ${runs} runs (models x the values of max_tokens, temperature, thinking and num_ctx), one after another.\n\n${state.estimateText()}\n\nStart them?`)) return;
  try { await startBench(body); } catch (e) { appendLog(`<span class="red">${esc(e.message)}</span>`); }
});
// "Live output": whether THIS page shows it (the server always streams it); remembered in the browser
$("t-liveout").checked = recall("live-output") !== "0";
$("t-liveout").addEventListener("change", async () => {
  store("live-output", $("t-liveout").checked ? "1" : "0");
  if (!$("t-liveout").checked) { renderOut({ id: state.out.id, title: "", think: "", text: "" }); return; }
  try {  // switched on: the request that is running is shown from its start
    const s = await api(`/api/bench?since=${state.benchSeq}&out=-1`);
    renderOut(s.out);
  } catch (e) { /* the next update fills it */ }
});
$("t-stop").addEventListener("click", () => api("/api/bench/stop", { method: "POST" }).catch(() => {}));

function renderSample(s) {
  if (!s) return '<span class="dim">No resource data (set an SSH host on the endpoint).</span>';
  const lines = s.gpus.map((g) => {
    const name = g.name.replace("NVIDIA ", "").replace("GeForce ", "").slice(0, 14).padEnd(14);
    return `GPU${g.index} ${esc(name)} ${fmt(g.util, 0, "%").padStart(4)}  ${(g.mem_used / 1024).toFixed(1).padStart(4)}/` +
      `${(g.mem_total / 1024).toFixed(0)}G${g.shared ? " shared" : ""} ${fmt(g.power, 0, "W").padStart(5)} ${fmt(g.temp, 0, "°C").padStart(5)}`;
  });
  if (!s.gpus.length) lines.push('<span class="yellow">No GPU found (nvidia-smi on the server?)</span>');
  lines.push(`CPU ${fmt(s.cpu_pct, 0, " %")}   RAM ${fmt((s.mem_used_mb || 0) / 1024, 1)}/${fmt((s.mem_total_mb || 0) / 1024, 0)} GB`);
  return lines.join("\n");
}

// live output of the running request; the server only sends what is new since (id, think/text length)
// blend an element out over three seconds, then run `then` (clearing it); showLive brings it back at once
const FADE_MS = 3000;
// Only the text blends out (its colour goes transparent), never the box itself: nothing but a few lines of text change, so the
// browser has nothing to redraw around them. When the text is gone the content is emptied without any visible effect.
function showLive(el) { clearTimeout(el._fade); el.classList.remove("fade"); }
function fadeLive(el, then) {
  clearTimeout(el._fade);
  el.classList.add("fade");
  el._fade = setTimeout(() => { then(); showLive(el); }, FADE_MS + 100);
}
function fadeOutput() {
  fadeLive($("out"), () => renderOut({ id: state.out.id, title: "", think: "", text: "" }));
  fadeLive($("out-tps"), () => { $("out-tps").textContent = ""; });
}

// A fast model sends hundreds of chunks per second, and every paint lays the whole output out again: painted chunk by chunk,
// a slow browser falls behind and the page hangs. So chunks pile up in a queue and are painted together, OUT_PAINT_MS apart;
// only the tail of the output is kept in the page (the box scrolls with the end anyway).
const OUT_PAINT_MS = 100, OUT_KEEP_CHARS = 40000;
const outQueue = { think: "", text: "", timer: 0 };
function paintOut() {
  outQueue.timer = 0;
  const o = { append: true, think: outQueue.think, text: outQueue.text };
  outQueue.think = outQueue.text = "";
  if (!o.think && !o.text) return;
  const box = $("out");
  follow(box, () => updateOut(o, box.querySelector(".think"), box.querySelector(".ans")));
}
function renderOut(o) {
  const box = $("out");
  if (o.append) {
    outQueue.think += o.think || ""; outQueue.text += o.text || "";
    if (!outQueue.timer) outQueue.timer = setTimeout(paintOut, OUT_PAINT_MS);
    return;
  }
  clearTimeout(outQueue.timer); outQueue.timer = 0; outQueue.think = outQueue.text = "";  // (a new request: what was waiting belongs to the old one)
  updateOut(o, box.querySelector(".think"), box.querySelector(".ans"));  // a new request starts at the end again
  box.dataset.follow = "1"; box.scrollTop = box.scrollHeight;
}

// what the line above the live output says: the request, or why there is nothing to see
const PHASE_NOTE = { warmup: "warming up the model - nothing to show yet", capabilities: "capability check - nothing to show",
                     load: "load test - nothing to show" };
state.phase = "";
function setOutLabel() {
  const note = state.outRunning && PHASE_NOTE[state.phase];
  $("out-title").classList.toggle("grey", !!note);
  $("out-label").textContent = !$("t-liveout").checked ? "off" : note || state.out.title || "single requests and long sessions";
}

// append to the text node instead of rebuilding the whole text (the output can have many thousand characters)
function addText(el, text) {
  if (el.lastChild && el.lastChild.nodeType === 3) el.lastChild.appendData(text);
  else el.append(text);
  const node = el.lastChild;
  if (node.length > OUT_KEEP_CHARS * 1.5) { node.deleteData(0, node.length - OUT_KEEP_CHARS); el.classList.add("trimmed"); }
}

function updateOut(o, think, ans) {
  if (!o.append) {
    think.textContent = ""; ans.textContent = "";
    think.classList.remove("trimmed"); ans.classList.remove("trimmed");
    if (o.title) { showLive($("out")); showLive($("out-tps")); }  // a new request: whatever was blending out is back at once
    state.out = { id: o.id, think: 0, text: 0, t0: 0, lastChars: 0, base: 0 };
    state.out.title = o.title || "";
    setOutLabel();
  }
  if (o.think) addText(think, o.think);
  if (o.text) {
    if (!state.out.text && think.textContent.trim()) ans.textContent = "\n\n----- answer -----\n\n";
    addText(ans, o.text);
  }
  // count code points like the server (Python), not UTF-16 units
  state.out.think += [...o.think].length; state.out.text += [...o.text].length;
  // what a non-append render brings is the output SO FAR (a new request, a page reload, Live output switched on again): it is
  // the starting point of the speed estimate, it must not count as streamed
  if (!o.append) state.out.base = state.out.lastChars = state.out.think + state.out.text;
  liveTps(o);
}

// rough running output speed shown above the live output: tokens ~ chars/4 (the client has no tokenizer), timed
// from the first character (so the TTFT wait is excluded) and only recomputed when new output actually arrived
// (so it does not decay to 0 between requests). Labelled "~" as an estimate; the exact tok/s is in the log.
function liveTps(o) {
  const live = $("out-tps");
  if (!o.append && !o.title) { live.textContent = ""; return; }  // idle / request ended
  const total = state.out.think + state.out.text;
  if (total <= (state.out.lastChars || 0)) return;  // no new output this poll: keep the last value
  if (!state.out.t0) state.out.t0 = performance.now();  // first streamed characters arrived: start timing here
  state.out.lastChars = total;
  const secs = (performance.now() - state.out.t0) / 1000;
  if (secs > 0.3) {
    showLive(live);
    live.textContent = `~${fmt((total - state.out.base) / 4 / secs, 0)} tok/s`;
    clearTimeout(state.tpsIdle);  // no new output for a moment = the request is over: the speed blends out
    state.tpsIdle = setTimeout(() => fadeLive(live, () => { live.textContent = ""; }), 1500);
  }
}

// The live output arrives over a stream (server-sent events): every chunk is shown as soon as the server has it. While
// the stream is open the poll below leaves the output alone; without it (a server that does not know the stream, a
// connection that broke) the poll follows the output as before.
// The stream is open only while a test runs and this page is in view (a browser allows six connections per server: a stream
// held open by every tab would starve the other requests).
let outSource = null;
function syncOutStream() {
  const want = !!state.outRunning && !document.hidden && !!window.EventSource;
  if (want && !outSource) openOutStream();
  else if (!want && outSource) { outSource.close(); outSource = null; state.outStream = false; }
}
function openOutStream() {
  const es = outSource = new EventSource("/api/bench/stream");
  es.onopen = () => { state.outStream = true; };
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (!$("t-liveout").checked) return;
    if (e.k === "start") renderOut({ id: e.id, title: e.title, think: e.think || "", text: e.text || "" });  // (the output so far comes with it)
    else renderOut({ append: true, think: e.think || "", text: e.text || "" });
  };
  es.onerror = () => {
    state.outStream = false;
    if (es.readyState === EventSource.CLOSED) outSource = null;  // (the next poll opens it again; otherwise the browser reconnects)
  };
}

let pollTimer = null;
async function pollBench() {
  clearTimeout(pollTimer);
  try {
    const o = state.out, streamed = state.outStream || !state.outRunning;  // (nothing to follow while no test runs)
    const s = await api(`/api/bench?since=${state.benchSeq}&out=${streamed ? -2 : o.id}&think=${o.think}&text=${o.text}`);
    if (s.seq < state.benchSeq) {  // a new test was started elsewhere: reload the whole log
      state.benchSeq = 0; $("log").innerHTML = "";
      return pollBench();
    }
    // the server's start id changed: it was restarted (maybe updated). A notice with a button, never a forced reload: it would
    // throw away what is typed in a form and interrupt reading
    if (state.boot && s.boot && s.boot !== state.boot) $("restart-bar").hidden = false;
    if (!state.boot) state.boot = s.boot;
    s.log.forEach((x) => appendLog(x.html));
    if (!streamed && !state.outStream && s.running && $("t-liveout").checked) renderOut(s.out);
    if (s.phase !== state.phase || state.outRunning !== s.running) {
      const wasQuiet = state.outRunning && PHASE_NOTE[state.phase], quiet = s.running && PHASE_NOTE[s.phase];
      if ((quiet && !wasQuiet) || (state.outRunning && !s.running)) fadeOutput();  // nothing streams now: the last output blends out
      state.phase = s.phase; state.outRunning = s.running; setOutLabel();
    }
    syncOutStream();
    state.benchSeq = s.seq;
    $("t-progress").max = Math.max(1, s.total); $("t-progress").value = s.done;
    $("t-count").textContent = s.total ? `${s.done} / ${s.total}` : "";
    $("live").innerHTML = renderSample(s.sample);
    $("t-start").disabled = s.running; $("t-stop").disabled = !s.running;
    $("bench-badge").classList.remove("red");
    $("bench-badge").textContent = s.running ? `test running (${s.done}/${s.total})` : s.other ? "test running in another UI" : "";
    $("t-other").hidden = !s.other;
    $("t-other").textContent = s.other ? s.other + " Its progress shows there; its results appear here when it is done." : "";
    checkRuns();
    if (state.benchRunning && !s.running) { loadRuns(); loadConfig(); }
    else if (s.running && s.run_id !== state.lastRunId) loadRuns();  // batch: each finished model shows up at once
    state.lastRunId = s.run_id;
    state.benchRunning = s.running;
    pollTimer = setTimeout(pollBench, s.running ? 500 : 5000);
  } catch (e) {
    $("bench-badge").textContent = "server not reachable"; $("bench-badge").classList.add("red");
    pollTimer = setTimeout(pollBench, 5000);
  }
}
$("restart-reload").addEventListener("click", () => location.reload());
