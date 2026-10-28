"use strict";
// Test tab: the model list - part of the page, loaded in the order of the <script> tags in index.html
// --------------------------------------------------------------------- test
async function loadModels() {
  const name = $("t-ep").value, box = $("t-models");
  state.modelsFor = name;
  store("endpoint", name);
  clearReuseNote();
  const hint = (t) => { box.innerHTML = `<span class="dim">${esc(t)}</span>`; updateStartLabel(); };
  if (!name) return hint("select endpoint first");
  hint("loading ...");
  try {
    const r = await api("/api/models?endpoint=" + encodeURIComponent(name));
    if (state.modelsFor !== name) return;
    if (!r.models.length) return hint("no models");
    const chosen = new Set(r.models.length > 1 ? r.selected : r.models);
    state.modelInfo = r.reasoning || {};
    state.modelCaps = r.capabilities || {};
    state.modelSort = null; state.modelDetails = {};
    box.innerHTML = modelHeaderHtml() + r.models.map((m, i) => modelRowHtml(m, chosen.has(m), i)).join("");
    modelRows().forEach((row) => { refreshRow(row); fillRow(row, (r.params || {})[row.dataset.model]); refreshRow(row); renderModelCaps(row); });  // (first pass: the levels the model has beyond the usual ones)
    initModelGrips();
    bindHeadCheck($("tm-head"), () => [...box.querySelectorAll("input.mchk")], () => { updateStartLabel(); });
    syncHeadCheck($("tm-head"), () => [...box.querySelectorAll("input.mchk")]);
    updateStartLabel();
    loadModelDetails(name, r.models);
    loadTiming(name);
  } catch (e) {
    hint("not reachable");
    appendLog(`<span class="red">${esc(e.message)}</span>`);
  }
}
// ---- the model list: one row per model with its own parameters (the same as Whole test; empty = like whole test), the Q
// button (reasoning query) and what is known about the model's reasoning
const THINK_CHOICES = [["", "Model default"], ["off", "Off"], ["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "X-High"]];
// a thinking level's name; levels the Q query found on a model (minimal, ultra, max ...) are named after themselves
const THINK_NAMES = Object.fromEntries(THINK_CHOICES);
const thinkName = (v) => THINK_NAMES[v] || (v ? v[0].toUpperCase() + v.slice(1) : "Model default");
state.modelInfo = {}; state.modelDetails = {}; state.modelCaps = {}; state.modelSort = null;
const HEAD_TIP = "Click repeatedly: all, inverted, none, then your own selection again.";
function modelHeaderHtml() {
  const h = (k, t, tip, cap) => `<span class="msort" data-sort="${k}"${cap ? " data-cap" : ""}${tipAttr(tip + "\n\n" + SORT_TIP)}><span class="lbl">${t}</span></span>`;
  return `<div class="mhdr"><div class="mh0"><input type="checkbox" id="tm-head"${tipAttr(HEAD_TIP)}>` +
    h("name", "Model", "Model name as the server lists it.") + "</div>" +
    h("size", "Size", "Parameter size of the model in billions (B) - declared by the server where it says; in italics: read from the model name.") +
    h("quant", "Quant.", "Quantization of the model (declared by the server where it says; in italics: read from the model name, a hint only).") +
    h("chat", "Chat", "Whether the model can chat (text in, text out): the server declares it (hollow check), or an earlier run showed it (solid check). A cross: the server says it cannot (embedding models). Sort: ascending puts the models that can first.", "cap") +
    h("vision", "Vis.", "Whether the model can read images: the server declares it (hollow check) or an earlier run tested it (solid check); a cross = it cannot; a dash = not known. Sort: ascending puts the models that can first.", "cap") +
    h("tools", "Tools", "Whether the model can call tools: the server declares it (hollow check) or an earlier run tested it (solid check); a cross = it cannot; a dash = not known. Sort: ascending puts the models that can first.", "cap") +
    h("reasoning", "Reasoning", "Whether the model reasons and which levels it accepts - the Q button asks it.") + "<span></span><span></span></div>";
}
// a parameter size always in billions ("134.52M" -> "0.13B"), the same rule as the server's (client.billions)
function billions(text) {
  const m = /^\s*([\d.]+)\s*([KMBT]?)\s*$/i.exec(text || "");
  if (!m) return text || "";
  const b = parseFloat(m[1]) * { "": 1e-9, K: 1e-6, M: 1e-3, B: 1, T: 1e3 }[m[2].toUpperCase()];
  return b.toFixed(b < 0.1 ? 3 : 2).replace(/0+$/, "").replace(/\.$/, "") + "B";
}
// the columns of the model list can be dragged like those of the tables: a grip at the right edge of a header cell, double click = automatic
const MODEL_COLS = ["--cn", "--cs", "--cq", "--cc", "--cv", "--ct", "--cr"];
function initModelGrips() {
  const box = $("t-models"), cells = [...box.querySelectorAll(".mhdr > :nth-child(-n+7)")];
  const apply = (w) => MODEL_COLS.forEach((v, i) => { if (w) box.style.setProperty(v, w[i] + "px"); else box.style.removeProperty(v); });
  try { const w = JSON.parse(recall("colw:models") || "null"); if (w && w.length === MODEL_COLS.length) apply(w); } catch (e) { /* no stored widths */ }
  cells.forEach((cell, i) => {
    const h = document.createElement("span");
    h.className = "rz";
    h.title = "Drag to change the column width; double click: automatic widths";
    cell.append(h);
    h.addEventListener("click", (ev) => ev.stopPropagation());
    h.addEventListener("dblclick", (ev) => { ev.stopPropagation(); try { localStorage.removeItem("colw:models"); } catch (e) { /* storage unavailable */ } apply(null); });
    h.addEventListener("pointerdown", (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      const widths = cells.map((c) => c.getBoundingClientRect().width), x0 = ev.clientX, w0 = widths[i];
      apply(widths);
      h.setPointerCapture(ev.pointerId); h.classList.add("active");
      const move = (e) => { widths[i] = Math.max(28, w0 + e.clientX - x0); apply(widths); };
      const up = () => { h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); h.classList.remove("active"); store("colw:models", JSON.stringify(widths.map(Math.round))); };
      h.addEventListener("pointermove", move); h.addEventListener("pointerup", up);
    });
  });
}
// "20.9B" / "134.52M" -> a number, so the list sorts by size
const sizeValue = (t) => { const m = /^([\d.]+)\s*([KMBT]?)/i.exec(t || ""); return m ? parseFloat(m[1]) * ({ K: 1e3, M: 1e6, B: 1e9, T: 1e12 }[m[2].toUpperCase()] || 1) : null; };
// what the list knows about a model's ability: [yes|no, "tested"|"claimed"] or null. A run that tested it counts most, then what the
// server declares (Ollama: /api/show), then what an earlier run's capability check said.
const CAP_DECLARED = { chat: "completion", vision: "vision", tools: "tools" };
function modelCap(model, key) {
  const hint = (state.modelCaps[model] || {})[key], declared = (state.modelDetails[model] || {}).capabilities;
  if (hint && hint[1] === "tested") return [hint[0], "tested"];
  if (Array.isArray(declared)) return [declared.includes(CAP_DECLARED[key]) ? "yes" : "no", "claimed"];
  return hint ? [hint[0], "claimed"] : null;
}
const CAP_RANK_LIST = { "yes tested": 1, "yes claimed": 2, "no claimed": 3, "no tested": 4 };  // ascending: the models that can first
function renderModelCaps(row) {
  const m = row.dataset.model;
  row.querySelectorAll(".mcap").forEach((el) => {
    const c = modelCap(m, el.dataset.cap), what = { chat: "Chat", vision: "Vision", tools: "Tool calling" }[el.dataset.cap];
    if (!c) { el.innerHTML = '<span class="dim" title="not known: the server does not declare it and no earlier run says">–</span>'; return; }
    const [v, how] = c, why = how === "tested" ? "an earlier run tested it" : (state.modelDetails[m] || {}).capabilities ? "the server declares it" : "an earlier run's capability check says so";
    el.innerHTML = v === "yes" ? `<span class="cbox ${how === "tested" ? "ok" : "hollow"}" title="${esc(`${what}: the model can do it (${why})`)}">✓</span>`
      : `<span class="xmark${how === "tested" ? " solid" : ""}" title="${esc(`${what}: not available (${why === "the server declares it" ? "the server does not list it" : why})`)}">✗</span>`;
  });
}
const MODEL_SORT_ACC = {
  chat: (r) => { const c = modelCap(r.name, "chat"); return c ? CAP_RANK_LIST[c.join(" ")] : null; },
  vision: (r) => { const c = modelCap(r.name, "vision"); return c ? CAP_RANK_LIST[c.join(" ")] : null; },
  tools: (r) => { const c = modelCap(r.name, "tools"); return c ? CAP_RANK_LIST[c.join(" ")] : null; },
  name: (r) => r.name, size: (r) => sizeValue(r.size), quant: (r) => r.quant || null,
  reasoning: (r) => { const i = state.modelInfo[r.name]; return i ? (i.reasons === false ? 1 : 2) : null; },
};
function sortModels() {
  const box = $("t-models"), rows = modelRows();
  const items = rows.map((row) => ({ row, name: row.dataset.model, index: +row.dataset.i, ...(state.modelDetails[row.dataset.model] || {}) }));
  const ordered = state.modelSort && state.modelSort.key ? sortBy(items, state.modelSort, MODEL_SORT_ACC) : items.sort((a, b) => a.index - b.index);
  ordered.forEach((it) => box.appendChild(it.row));
  box.querySelectorAll(".mhdr .msort").forEach((h) => { h.dataset.dir = state.modelSort && state.modelSort.key === h.dataset.sort ? (state.modelSort.desc ? "desc" : "asc") : ""; });
}
$("t-models").addEventListener("click", (ev) => {
  const h = ev.target.closest(".mhdr .msort");
  if (!h) return;
  state.modelSort = nextSort(state.modelSort, h.dataset.sort);
  sortModels();
});
// quantization and size of every listed model (asked in the background: the list is usable at once)
async function loadModelDetails(name, models) {
  try {
    const q = models.map((m) => "&model=" + encodeURIComponent(m)).join("");
    const r = await api("/api/models/details?endpoint=" + encodeURIComponent(name) + q);
    if (state.modelsFor !== name) return;
    state.modelDetails = r.details || {};
    const SOURCE = { declared: "stated by the server", guessed: "guessed from the model name", "command line": "from the server's command line" };
    modelRows().forEach((row) => {
      const d = state.modelDetails[row.dataset.model] || {};
      for (const [cls, key] of [["msize", "size"], ["mquant", "quant"]]) {
        const el = row.querySelector("." + cls);
        el.textContent = (key === "size" ? billions(d[key]) : d[key]) || "-";
        el.classList.toggle("guess", d[key + "_source"] === "guessed");
        el.title = d[key] ? `${key === "size" ? billions(d[key]) : d[key]} - ${SOURCE[d[key + "_source"]] || ""}` : "not known";
      }
      renderModelCaps(row);
    });
    refreshNumCtx();
    if (state.modelSort && ["size", "quant", "chat", "vision", "tools"].includes(state.modelSort.key)) sortModels();
  } catch (e) { /* the columns stay empty: the list works without them */ }
}
const modelRows = () => [...$("t-models").querySelectorAll(".mrow")];
const isOllama = () => ["ollama", "openwebui"].includes(((state.cfg.endpoints || []).find((e) => e.name === $("t-ep").value) || {}).kind);
const splitValues = (t) => String(t).split(/[,;]/).map((x) => x.trim()).filter(Boolean);
function modelRowHtml(m, checked, index = 0) {
  const think = THINK_CHOICES.map(([v, t]) => `<label><input type="checkbox" data-think value="${v}">${t}</label>`).join("");
  return `<div class="mrow" data-model="${esc(m)}" data-i="${index}"><div class="mhead">` +
    `<label title="${esc(m)}"><input type="checkbox" class="mchk" value="${esc(m)}"${checked ? " checked" : ""}>${esc(m)}</label>` +
    `<span class="msize dim">-</span><span class="mquant dim">-</span>` +
    ["chat", "vision", "tools"].map((k) => `<span class="mcap" data-cap="${k}"></span>`).join("") + `<span class="msum dim"></span>` +
    `<button type="button" class="mbtn mq"${tipAttr(TIPS["t-mq"])}>Q</button>` +
    `<button type="button" class="mbtn mp"${tipAttr(TIPS["t-mp"])}>params</button></div>` +
    `<div class="mpanel" hidden><div class="fields">` +
    `<label>Temperature<input data-p="temperature" placeholder="like whole test"></label>` +
    `<div class="mf">Thinking<details class="tdd"><summary></summary><div class="tlist"><label><input type="checkbox" data-think value="__inherit">like whole test</label>${think}</div></details></div>` +
    `<label class="mp-numctx">num_ctx<input data-p="num_ctx" placeholder="like whole test"></label>` +
    `<label>max_tokens <span class="dim">(NOT the context size!)</span><input data-p="max_tokens" placeholder="like whole test"></label>` +
    `<label>Timeout (s)<input data-p="request_timeout" placeholder="like whole test"></label>` +
    `</div><div class="mnote dim"></div></div></div>`;
}
function fillRow(row, p) {
  for (const [k, v] of Object.entries(p || {})) {
    if (k === "thinking") { const vals = [].concat(v).map(String); row.querySelectorAll("[data-think]").forEach((i) => { i.checked = vals.includes(i.value); }); }
    else { const i = row.querySelector(`input[data-p=${k}]`); if (i) i.value = v; }
  }
}
const ownThink = (row) => [...row.querySelectorAll("[data-think]:checked")].map((i) => i.value).filter((v) => v !== "__inherit");  // [] = like whole test
function ownParams(row) {  // what the user set for this model (remembered with the next start)
  const out = {};
  row.querySelectorAll("input[data-p]:not(:disabled)").forEach((i) => { if (i.value.trim()) out[i.dataset.p] = i.value.trim(); });  // (a greyed-out value is the server's, not a setting)
  const t = ownThink(row);
  if (t.length) out.thinking = t;
  return out;
}
// the thinking settings a model runs with: its own or the whole test's, narrowed to what the model accepts once that is known
function effectiveThinking(row) {
  const own = ownThink(row), vals = own.length ? own : thinkValues(), info = state.modelInfo[row.dataset.model];
  if (info && info.reasons === false) return [""];
  if (info && Array.isArray(info.efforts)) { const ok = vals.filter((v) => v === "" || info.efforts.includes(v)); return ok.length ? ok : [""]; }
  return vals;
}
function effectiveValues(row) {
  const own = ownParams(row), list = (k, id) => (own[k] ? splitValues(own[k]) : fieldValues(id));
  return { max_tokens: list("max_tokens", "t-maxtok"), temperature: list("temperature", "t-temp"), num_ctx: list("num_ctx", "t-numctx"),
           request_timeout: list("request_timeout", "t-timeout"), thinking: effectiveThinking(row) };
}
const INFO_SOURCE = { query: "asked", declared: "declared by the server" };
function refreshRow(row) {
  const m = row.dataset.model, info = state.modelInfo[m], own = ownParams(row), t = ownThink(row);
  if (info && Array.isArray(info.efforts)) {  // levels this model accepts beyond the usual ones (found by the Q query)
    for (const e of info.efforts) {
      if (!row.querySelector(`[data-think][value="${CSS.escape(e)}"]`)) {
        row.querySelector(".tlist").insertAdjacentHTML("beforeend", `<label><input type="checkbox" data-think value="${esc(e)}">${esc(thinkName(e))}</label>`);
      }
    }
  }
  row.querySelector("[data-think][value=__inherit]").checked = !t.length;
  row.querySelectorAll("[data-think]").forEach((i) => {  // what the model does not accept is greyed out with the reason (everything is offered while it is not known)
    const v = i.value, lab = i.closest("label");
    let why = "";
    if (v !== "__inherit" && v !== "" && info) {
      if (info.reasons === false) why = "The model does not reason: thinking levels have no effect.";
      else if (v === "off" && info.off_ignored) why = "The model reasons even when thinking is switched off - this choice has no effect.";
      else if (Array.isArray(info.efforts) && !info.efforts.includes(v)) why = "The model does not accept this level (asked with the Q button).";
    }
    i.disabled = !!why; lab.classList.toggle("na", !!why); lab.title = why;
    if (why) i.checked = false;
  });
  const now = ownThink(row);
  row.querySelector(".tdd > summary").textContent = now.length ? now.map(thinkName).join(" + ") : "like whole test";
  const parts = [own.max_tokens && `max_tokens ${own.max_tokens}`, own.temperature && `temp ${own.temperature}`, now.length && `thinking ${now.map(thinkName).join("+")}`,
    own.num_ctx && isOllama() && `num_ctx ${own.num_ctx}`, own.request_timeout && `timeout ${own.request_timeout}`].filter(Boolean);
  const levels = info && Array.isArray(info.efforts) ? info.efforts.filter((e) => e !== "off").join(", ") : "";
  const known = info ? (info.reasons === false ? "no" : ["yes" + (levels ? "" : " (earlier run)"), info.off_ignored ? "always on" : "", levels].filter(Boolean).join(" · ")) : "-";
  row.querySelector(".msum").textContent = known;
  row.querySelector(".msum").title = info ? `Reasoning: ${info.reasons ? "yes" : "no"} (${INFO_SOURCE[info.source] || info.source})${info.off_ignored ? "\nAlways on: it reasons even when thinking is switched off." : ""}${info.note ? " - " + info.note : ""}` : "Not asked yet - the Q button asks the model.";
  row.querySelector(".mp").classList.toggle("set", !!parts.length);
  row.querySelector(".mp").title = TIPS["t-mp"] + (parts.length ? "\n\nSet for this model: " + parts.join(" · ") : "");
  const q = row.querySelector(".mq");
  q.classList.toggle("known", !!info && (info.source === "query" || info.source === "declared"));
  const eff = effectiveThinking(row), want = now.length ? now : thinkValues();
  const note = row.querySelector(".mnote");
  if (note.dataset.err !== "1") {
    note.textContent = info ? `Reasoning: ${info.reasons ? "yes" : "no"} (${INFO_SOURCE[info.source] || info.source})${info.note ? " - " + info.note : ""}.` +
      (JSON.stringify(eff) !== JSON.stringify(want) ? ` Run with: ${eff.map(thinkName).join(", ")} instead of ${want.map(thinkName).join(", ")}.` : "") : "";
  }
}
async function queryReasoning(row) {
  const btn = row.querySelector(".mq"), note = row.querySelector(".mnote");
  btn.disabled = true; btn.textContent = "...";
  note.dataset.err = ""; note.className = "mnote dim";
  try {
    state.modelInfo[row.dataset.model] = (await api("/api/models/reasoning", { method: "POST", body: { endpoint: $("t-ep").value, model: row.dataset.model } })).reasoning;
  } catch (e) {
    note.dataset.err = "1"; note.className = "mnote red"; note.textContent = e.message;
    row.querySelector(".mpanel").hidden = false;
  }
  btn.disabled = false; btn.textContent = "Q";
  refreshRow(row);
  updateStartLabel();
}
$("t-models").addEventListener("click", (ev) => {
  const row = ev.target.closest(".mrow");
  if (!row) return;
  if (ev.target.closest(".mp")) row.querySelector(".mpanel").hidden = !row.querySelector(".mpanel").hidden;
  else if (ev.target.closest(".mq")) queryReasoning(row);
});
function modelChanged(ev) {
  const row = ev.target.closest(".mrow"), t = ev.target;
  if (!row) return;
  if (t.matches("[data-think]") && t.checked) {  // "like whole test" excludes the others
    row.querySelectorAll("[data-think]").forEach((i) => { if (i !== t && (t.value === "__inherit" || i.value === "__inherit")) i.checked = false; });
  }
  row.querySelector(".mnote").dataset.err = "";
  refreshRow(row);
  updateStartLabel();
}
$("t-models").addEventListener("input", (ev) => { if (ev.target.matches("input[data-p]")) modelChanged(ev); });
$("t-models").addEventListener("change", (ev) => { if (ev.target.matches("[data-think]")) modelChanged(ev); });
["t-maxtok", "t-temp", "t-numctx", "t-timeout", "t-think"].forEach((id) => $(id).addEventListener(id === "t-think" ? "change" : "input", () => modelRows().forEach(refreshRow)));

function resetModelParams() {
  modelRows().forEach((row) => {
    row.querySelectorAll("input[data-p]").forEach((i) => { i.value = ""; });
    row.querySelectorAll("[data-think]").forEach((i) => { i.checked = false; });
    row.querySelector(".mnote").dataset.err = "";
    refreshRow(row);
  });
  updateStartLabel();
}
$("tm-reset").addEventListener("click", resetModelParams);

function checkedModels() { return [...$("t-models").querySelectorAll("input.mchk:checked")].map((i) => i.value); }
