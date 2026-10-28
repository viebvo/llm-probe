"use strict";
// Responses - part of the page, loaded in the order of the <script> tags in index.html
// ---------------------------------------------------------------- responses
// the runs whose responses the Responses tab shows (chosen in Results / Compare, independent of the comparison)
function showResponses(ids) {
  setAnsRuns(ids);  // persists, renders the picker and the responses
  showTab("ans", true);
}
const renderResponses = () => busy(renderResponsesNow);
async function renderResponsesNow() {
  const ids = state.ansRuns;
  state.ansRepsKey = null;  // run selection changed: the per-prompt reps cache is stale
  if (!ids.length) {
    state.answers = null;
    $("ans-runs").textContent = "";
  } else {
    const c = await api("/api/responses?runs=" + ids.join(","));
    state.answers = { runs: c.runs.map((run, n) => ({ i: n, run, ...c.responses[n] })) };
    $("ans-runs").innerHTML = state.answers.runs.map((r) => `<b class="c${r.i}">#${r.run.id}</b> ${esc(r.run.model)} ` +
      `<span class="dim">(${esc(r.run.endpoint)}, ${esc(r.run.started.slice(0, 16).replace("T", " "))})</span>`).join("<br>");
  }
  renderAnswerSelect();
}
function renderAnswerSelect() {
  if (!state.answers) {
    state.ansOptions = [];
  } else {
    // every prompt that at least one run tested (status has all single-phase prompts, successful or not)
    const ids = [...new Set(state.answers.runs.flatMap((r) => Object.keys(r.status || {})))];
    state.ansOptions = ids.map((p) => [promptName(p), p]);
  }
  let first = false;
  if (state.ansPreset) {  // "show relevant prompts" of a tile: narrow the list to the prompts of that figure
    const test = GAUGE_PROMPTS[state.ansPreset], ids = new Set(state.ansOptions.map(([, id]) => id));
    const hit = state.prompts.filter((p) => ids.has(p.id) && test(p)).map((p) => p.id);
    state.ansSet = hit.length ? new Set(hit) : null;
    state.ansPreset = null; first = !!hit.length;
  }
  renderAnsSet();
  applyAnsFilter();  // fills the dropdown (respecting the filter and the set) and renders the current selection
  if (first) { const sel = $("ans-prompt"); if (sel.options.length) { sel.value = sel.options[0].value; renderAnswers(); } }
}
// the pre-selection list: tick the prompts the dropdown below offers (null = all of them)
state.ansSet = null;
function renderAnsSet() {
  const all = state.ansOptions || [], box = $("ans-set"), list = box.querySelector(".tlist");
  const groups = ["quality", "reliability", "performance"].filter((g) => all.some(([, id]) => promptGroup(id) === g));
  list.innerHTML = '<div class="presets">' + ["all", "none", ...groups].map((g) => `<a href="#" data-preset="${g}">${g === "performance" ? "speed" : g}</a>`).join(" · ") + "</div>" +
    all.map(([name, id]) => `<label title="${esc(id)}"><input type="checkbox" value="${esc(id)}"${!state.ansSet || state.ansSet.has(id) ? " checked" : ""}>${esc(name)}</label>`).join("");
  box.querySelector("summary").textContent = !all.length ? "no runs chosen" : state.ansSet ? `${[...state.ansSet].filter((id) => all.some(([, x]) => x === id)).length} of ${all.length} prompts` : `all ${all.length} prompts`;
}
$("ans-set").addEventListener("change", (e) => {
  if (!e.target.matches("input")) return;
  const ids = [...$("ans-set").querySelectorAll("input")];
  state.ansSet = ids.every((i) => i.checked) ? null : new Set(ids.filter((i) => i.checked).map((i) => i.value));
  renderAnsSet(); applyAnsFilter();
});
$("ans-set").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-preset]");
  if (!a) return;
  e.preventDefault();
  const g = a.dataset.preset, all = (state.ansOptions || []).map(([, id]) => id);
  state.ansSet = g === "all" ? null : new Set(g === "none" ? [] : all.filter((id) => promptGroup(id) === g));
  renderAnsSet(); applyAnsFilter();
});
// narrow the prompt dropdown by substring (name or id). Only an explicit pick (change event) loads responses - a
// keystroke never triggers the ~350 KB per-prompt fetch; a filtered-out selection falls back to a placeholder.
function applyAnsFilter() {
  const sel = $("ans-prompt"), q = ($("ans-filter").value || "").trim().toLowerCase();
  const all = state.ansOptions || [];
  if (!all.length) {
    fillSelect(sel, [], "choose runs above or in Results (Show responses)"); sel.size = 1;
    renderAnswers(); return;
  }
  const inSet = state.ansSet ? all.filter(([, id]) => state.ansSet.has(id)) : all;
  const opts = q ? inSet.filter(([name, id]) => (name + " " + id).toLowerCase().includes(q)) : inSet;
  const prev = sel.value;
  fillSelect(sel, opts, q ? `${opts.length} match${opts.length === 1 ? "" : "es"} - pick one` : "");
  sel.style.minWidth = `min(100%, ${Math.min(80, Math.max(24, ...opts.map(([name]) => name.length + 4)))}ch)`;  // wide enough for the longest title
  // show it as a short open listbox while a filter is active, so the list visibly shrinks; collapse it otherwise
  sel.size = q ? Math.min(Math.max(opts.length, 2), 12) : 1;
  if (sel.value !== prev) renderAnswers();  // selection kept -> no reload; dropped to placeholder -> clears the view
}
const renderAnswers = () => busy(renderAnswersNow);
async function renderAnswersNow() {
  const pid = $("ans-prompt").value, a = state.answers, grid = $("ans-grid");
  if (!a) { grid.innerHTML = ""; $("ans-images").innerHTML = ""; return; }
  const images = (lookupPrompt(pid) || { images: [] }).images;
  $("ans-question").innerHTML = pid ? questionHtml(lookupPrompt(pid)) : "";
  $("ans-images").innerHTML = images.map((f) => `<a href="/api/images/${encodeURIComponent(f)}" target="_blank" ` +
    `title="${esc(f)}: the image sent with this prompt - click to open it in full size"><img src="/api/images/${encodeURIComponent(f)}" alt="${esc(f)}"></a>`).join("");
  if (!pid) { grid.innerHTML = ""; grid.style.gridTemplateColumns = ""; return; }
  const runs = a.runs, key = runs.map((r) => r.run.id).join(",") + "|" + pid;
  if (state.ansRepsKey !== key) {  // load this prompt's repetitions (heavy response texts) on demand
    grid.innerHTML = '<span class="dim">Loading responses ...</span>';
    try {
      const c = await api("/api/responses?runs=" + runs.map((r) => r.run.id).join(",") + "&prompt=" + encodeURIComponent(pid));
      if ($("ans-prompt").value !== pid) return;  // the user switched prompt while loading
      runs.forEach((r, n) => { r.reps = (c.responses[n] || {}).reps || {}; });
      state.ansRepsKey = key;
    } catch (e) { grid.innerHTML = '<span class="dim">Could not load the responses.</span>'; return; }
  }
  const repsOf = (r) => (r.reps || {})[pid] || [];
  // one scale across all runs and repetitions; the tok/s bar starts at 0 so the bars are directly comparable
  const nReps = Math.max(0, ...runs.map((r) => repsOf(r).length));
  const tmax = Math.max(0, ...runs.flatMap((r) => repsOf(r).map((x) => x.tokens || 0)));
  const tpsMax = Math.max(0, ...runs.flatMap((r) => repsOf(r).map((x) => x.decode_tps || 0))) || 1;
  // columns = runs (fixed, so repetition N lines up across runs); grid fills row by row (run order within a row)
  grid.style.gridTemplateColumns = `repeat(${runs.length}, minmax(0, 1fr))`;
  let html = runs.map((r) => {
    const tps = repsOf(r).map((x) => x.decode_tps).filter((v) => v != null);
    return `<div class="anshdr" data-run="${r.i}"><span class="c${r.i}">run #${r.run.id}</span> ` +
      `<span class="dim" style="font-weight:normal">${esc(r.run.model)}</span>${dotsGraph(tps, tpsMax, r.i)}</div>`;
  }).join("");
  for (let k = 0; k < Math.max(1, nReps); k++) {
    html += runs.map((r) => {
      const list = repsOf(r);
      if (!list.length) {  // this run produced no successful response for the prompt
        if (k > 0) return `<div class="repcell" data-run="${r.i}"></div>`;  // the note sits in the first row only
        const st = (r.status || {})[pid];  // no status entry = the prompt was not part of this run
        const note = !st ? "(test not run)" : st.error ? "(no response - " + st.error + ")" : "(no response)";
        return `<div class="repcell" data-run="${r.i}"><pre class="answer ans-note">${esc(note)}</pre></div>`;
      }
      const m = list[k];
      if (!m) return `<div class="repcell" data-run="${r.i}"><div class="rhead dim">repetition ${k + 1}: not run</div></div>`;
      return `<div class="repcell" data-run="${r.i}"><div class="rhead">${responseHead(r.i, m, tmax)}</div>` +
        `<pre class="answer">${esc(m.text || "(empty)")}</pre></div>`;
    }).join("");
  }
  grid.innerHTML = html;
  applyAnsHl(ansPinIndex());
}
// the question the responses answer, above them: the prompt text; a long session lists its requests; a prompt generated for
// each run (sized to the context) shows its template
function questionHtml(p) {
  if (!p) return "";
  const session = p.kind === "deviation";  // (p.session is an empty object for the others)
  const text = session ? (p.steps || []).map((t, k) => `${k + 1}. ${t}`).join("\n\n") || p.text : p.text;
  const note = p.adaptive ? ' <span class="dim">(generated for each run: its size follows the context the model is served with)</span>' : "";
  return `<div class="ans-q"><b>Question${session ? "s (the steps of the session)" : ""}:</b>${note}<pre class="answer">${esc(text || "")}</pre></div>`;
}
// Responses: the same highlight as in Compare - hover a run's column to fade the others, a click pins it (a second click, or the
// header again, releases it)
state.ansPin = null;
const ansPinIndex = () => (state.answers ? state.answers.runs.findIndex((r) => r.run.id === state.ansPin) : -1);
function applyAnsHl(idx) {
  const on = idx != null && idx >= 0;
  $("ans-grid").querySelectorAll("[data-run]").forEach((e) => {
    const me = on && +e.dataset.run === idx;
    e.classList.toggle("hl-col", me);
    e.classList.toggle("hl-dim", on && !me);
  });
}
{
  const grid = $("ans-grid"), col = (e) => (e.target.closest ? e.target.closest("[data-run]") : null);
  grid.addEventListener("mouseover", (e) => { if (state.ansPin == null) { const t = col(e); applyAnsHl(t ? +t.dataset.run : null); } });
  grid.addEventListener("mouseleave", () => { if (state.ansPin == null) applyAnsHl(null); });
  grid.addEventListener("click", (e) => {
    if (e.target.closest("button, a, input, select, label, summary") || String(window.getSelection && window.getSelection()).trim()) return;
    const t = col(e);
    if (!t || !state.answers) return;
    const run = state.answers.runs.find((r) => r.i === +t.dataset.run);
    state.ansPin = run && state.ansPin !== run.run.id ? run.run.id : null;
    applyAnsHl(ansPinIndex());
  });
}

// summary above one repetition's response: its number, tokens, reasoning/answer share, timing, cut marker,
// a 0-based decode-tok/s bar and the answer check
function responseHead(color, m, tmax) {
  const share = (m.share || 0) * 100;
  const len = tmax && m.tokens ? Math.max(2, m.tokens / tmax * 100) : 100;
  let out = `<div><b>repetition ${m.rep + 1}</b> · ${fmt(m.tokens, 0)} tokens · reasoning ${fmt(share, 0, " %")} · ` +
    `first token ${fmt(m.ttft, 2, " s")}` + (m.ttfa != null ? ` · answer after ${fmt(m.ttfa, 2, " s")}` : "") +
    ` · total ${fmt(m.total, 1, " s")}` + (m.cut ? ` · ${cutMark(m.cut)}` : "") + "</div>" +
    `<div class="segs" style="width:${len.toFixed(1)}%" title="Length: ${fmt(m.tokens, 0)} output tokens, relative to the longest response of this prompt (${fmt(tmax, 0)} tokens).\nReasoning ${fmt(share, 0, " %")}, answer ${fmt(100 - share, 0, " %")} of the response (by characters)">` +
    (share > 0 ? `<div class="seg-think" style="width:${share.toFixed(1)}%"></div>` : "") +
    `<div class="f${color}" style="width:${(100 - share).toFixed(1)}%"></div></div>` +
    `<div class="dim" style="font-size:11px">${fmt(m.decode_tps)} tok/s</div>`;
  if (m.check) out += `<div>Check: ${checkVerdict(m.check)}</div>${checkSquares(m.check)}`;
  return out;
}
// per-run tok/s spread over its repetitions, drawn on a 0-based scale (so the dots are comparable across runs)
function dotsGraph(tpsList, tpsMax, color) {
  if (!tpsList.length) return "";
  const pos = (v) => Math.max(2, Math.min(98, v / tpsMax * 100));
  return `<div class="dots" title="Decode tok/s of each repetition (scale 0 - ${fmt(tpsMax)} tok/s)">` +
    tpsList.map((v, n) => {
      const near = tpsList.slice(0, n).filter((w) => Math.abs(pos(w) - pos(v)) < 2).length;
      return `<span class="f${color}" style="left:${pos(v).toFixed(1)}%;top:${near % 2 ? 6 : near ? 0 : 3}px" ` +
        `title="repetition ${n + 1}: ${fmt(v)} tok/s"></span>`;
    }).join("") +
    `</div><div class="dim" style="display:flex;justify-content:space-between;font-size:11px">` +
    `<span>0</span><span>tok/s</span><span>${fmt(tpsMax)}</span></div>`;
}
$("ans-prompt").addEventListener("change", () => { $("ans-prompt").size = 1; renderAnswers(); });
$("ans-filter").addEventListener("input", applyAnsFilter);

