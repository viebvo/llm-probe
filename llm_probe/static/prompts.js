"use strict";
// Prompts - part of the page, loaded in the order of the <script> tags in index.html
// ------------------------------------------------------------------ prompts
// sort order of the prompts list: null = catalog order until a header is clicked
state.promptMarked = new Set();
state.promptSort = (() => { try { return JSON.parse(recall("prompt-sort")) || null; } catch (e) { return null; } })();
const PROMPT_SORT_ACC = { difficulty: (p) => p.difficulty || "easy", lang: (p) => p.lang || "",
                          needs: (p) => p.needs.join(", "), chars: (p) => p.text.length };
async function loadPrompts() {
  const [all, selection] = await Promise.all([api("/api/prompts"), api("/api/selection")]);
  setPrompts(all); state.selection = selection;
  if (state.selection.version) $("app-version").textContent = "v" + state.selection.version;
  $("foot-ver").textContent = state.selection.version ? "v" + state.selection.version : "";
  if (state.selection.promptset) $("pr-promptset").innerHTML = `Built-in prompt set <b>v${esc(state.selection.promptset)}</b> (program v${esc(state.selection.version || "?")}). Built-in prompts are read-only; your own prompts are listed with category "custom".`;
  renderPromptTable();
  promptsFilter.render();
  renderPromptChecklist();
  renderPromptImages();
}
// the ticked custom prompts (the export takes these); built-in prompts have a greyed-out box
const promptMarkBoxes = () => [...$("pr-table").querySelectorAll("td.mark input")];
const shownMarkBoxes = () => promptMarkBoxes().filter((i) => !i.closest("tr").hidden);  // the header check box acts on what the filter shows
// the same filter as in the Test tab (own state): it hides rows, the marks stay
const promptsFilter = makePromptFilter({
  box: $("pr-facets"),
  rows: () => [...$("pr-table").querySelectorAll("tbody tr[data-id]")].map((tr) => [tr, state.prompts.find((p) => p.id === tr.dataset.id)]),
  onApply: () => syncHeadCheck($("pr-head"), shownMarkBoxes),
});
function syncPromptMarks() {
  state.promptMarked = new Set(promptMarkBoxes().filter((i) => i.checked && !i.disabled).map((i) => i.value));
  syncHeadCheck($("pr-head"), shownMarkBoxes);
  const n = state.promptMarked.size;
  $("pr-export").disabled = !n;
  $("pr-export").textContent = n ? `Export marked custom prompts (${n})` : "Export marked custom prompts";
}
bindHeadCheck($("pr-head"), shownMarkBoxes, syncPromptMarks);
$("pr-table").addEventListener("change", (ev) => { if (ev.target.matches("td.mark input")) syncPromptMarks(); });
function renderPromptTable() {
  const tb = $("pr-table").querySelector("tbody");
  const rows = sortBy([...state.prompts], state.promptSort, PROMPT_SORT_ACC);
  $("pr-table").querySelectorAll("th[data-sort]").forEach((th) => {
    th.dataset.dir = state.promptSort && th.dataset.sort === state.promptSort.key ? (state.promptSort.desc ? "desc" : "asc") : "";
  });
  tb.innerHTML = rows.map((p) =>
    `<tr class="click${p.id === state.promptBase ? " sel" : ""}" data-id="${esc(p.id)}"${tipAttr(promptPreview(p))}><td class="mark"><input type="checkbox" value="${esc(p.id)}"${p.builtin ? ' disabled title="Built-in prompts cannot be exported (only your own)"' : state.promptMarked.has(p.id) ? " checked" : ""}></td>` +
    `<td>${esc(p.id)}</td><td class="l">${esc(p.name)}</td>` +
    `<td class="l">${esc(p.group)}</td><td class="l">${esc(p.lang || "-")}</td><td class="l">${esc(p.difficulty || "easy")}</td>` +
    `<td class="l">${esc(p.category)}${p.builtin ? ' <span class="dim">(built-in)</span>' : ""}${isCodeBased(p) ? ' <span class="dim" title="Code-based prompt (a long coding session): it is generated in code and cannot be edited or recreated in this form.">(code)</span>' : ""}${p.adaptive ? ' <span class="dim" title="Generated when a run starts, sized to the context the model is served with; it cannot be edited or copied.">(generated)</span>' : ""}</td>` +
    `<td class="l">${esc(p.needs.join(", ") || "-")}</td><td>${p.text.length}</td></tr>`).join("");
  promptsFilter.apply();
  syncPromptMarks();
  tb.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", (ev) => {
    if (ev.target.closest("td.mark")) return;  // the check box marks the prompt for export; it does not open it
    const p = state.prompts.find((x) => x.id === tr.dataset.id);
    tb.querySelectorAll("tr").forEach((t) => t.classList.toggle("sel", t === tr));
    $("pr-id").value = p.id; $("pr-name").value = p.name; $("pr-cat").value = p.category; $("pr-text").value = p.text;
    $("pr-lang").value = p.lang || ""; $("pr-difficulty").value = p.difficulty || "easy";
    $("pr-expected").value = p.expected; $("pr-check").value = p.check || "";
    state.promptImages = [...p.images]; renderPromptImages();
    const extra = promptExtras(p, false);
    $("pr-extra").textContent = extra ? `Also sends: ${extra} (kept when saved under a new ID)` : "";
    state.promptBase = p.id;
    setPromptEditorLock(p);
  }));
}
// a small modal question with buttons (and an optional "apply to all" checkbox); resolves { value, all }
function askChoice({ title, html, buttons, all = "" }) {
  const dlg = $("dlg");
  $("dlg-title").textContent = title; $("dlg-body").innerHTML = html;
  $("dlg-all").hidden = !all; $("dlg-all").querySelector("span").textContent = all; $("dlg-all-box").checked = false;
  $("dlg-buttons").innerHTML = buttons.map((b, i) => `<button class="btn${b.cls ? " " + b.cls : ""}" data-i="${i}">${esc(b.label)}</button>`).join("");
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => { if (done) return; done = true; dlg.close(); resolve({ value, all: $("dlg-all-box").checked }); };
    $("dlg-buttons").querySelectorAll("button").forEach((btn) => btn.addEventListener("click", () => finish(buttons[+btn.dataset.i].value)));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish("cancel"); }, { once: true });  // Esc
    dlg.showModal();
  });
}
$("pr-export").addEventListener("click", async () => {
  try {
    const ids = [...state.promptMarked];
    const r = await fetch("/api/prompts/export?" + ids.map((id) => "id=" + encodeURIComponent(id)).join("&"));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const name = ((r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/) || [])[1] || "llm-probe-prompts.json";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await r.blob()), download: name });
    document.body.append(a); a.click(); a.remove();
    setMsg($("pr-status"), `Exported ${ids.length} custom prompt${ids.length === 1 ? "" : "s"} to ${name}.`, "ok");
  } catch (e) { setMsg($("pr-status"), "Export failed: " + e.message, "err"); }
});
$("pr-import").addEventListener("click", () => $("pr-import-file").click());
$("pr-import-file").addEventListener("change", async () => {
  const file = $("pr-import-file").files[0];
  $("pr-import-file").value = "";
  if (file) await importPromptFile(file);
});
async function importPromptFile(file) {
  const fail = (m) => setMsg($("pr-status"), m, "err");
  let data, plan;
  try { data = JSON.parse(await file.text()); } catch (e) { return fail(`${file.name} is not a JSON file.`); }
  try { plan = await api("/api/prompts/import/plan", { method: "POST", body: { data } }); } catch (e) { return fail(e.message); }
  if (!plan.count) return fail(`Nothing to import from ${file.name}` + (plan.problems.length ? ": " + plan.problems.join("; ") : "."));
  let mode = "add";
  if (plan.existing_custom) {
    const a = await askChoice({ title: "Import custom prompts",
      html: `<b>${plan.count}</b> prompt${plan.count === 1 ? "" : "s"} in <code>${esc(file.name)}</code>; you already have <b>${plan.existing_custom}</b> custom prompt${plan.existing_custom === 1 ? "" : "s"}.` +
        (plan.problems.length ? `<br><span class="yellow">Left out: ${esc(plan.problems.join("; "))}</span>` : ""),
      buttons: [{ label: "Add to my prompts", value: "add", cls: "primary" }, { label: "Replace all my custom prompts", value: "replace", cls: "danger" }, { label: "Cancel", value: "cancel" }] });
    if (a.value === "cancel") return setMsg($("pr-status"), "Import cancelled.");
    mode = a.value;
  }
  const conflicts = plan.conflicts.filter((c) => c.with === "builtin" || mode === "add");
  const resolutions = {}, remembered = {};
  for (const c of conflicts) {
    let action = remembered[c.with];
    if (!action) {
      const more = conflicts.filter((x) => x.with === c.with).length > 1;
      const a = await askChoice({ title: c.with === "builtin" ? "ID of a built-in prompt" : "Prompt already exists",
        html: c.with === "builtin" ? `<code>${esc(c.id)}</code> (${esc(c.name)}) is the ID of a built-in prompt, which cannot be overwritten.`
          : `A custom prompt with the ID <code>${esc(c.id)}</code> (${esc(c.name)}) already exists.`,
        buttons: [...(c.with === "custom" ? [{ label: "Overwrite", value: "overwrite", cls: "danger" }] : []),
          { label: "Rename (add _2 / (2))", value: "rename", cls: "primary" }, { label: "Skip", value: "skip" }, { label: "Cancel import", value: "cancel" }],
        all: more ? "Do the same for all further " + (c.with === "builtin" ? "built-in" : "custom") + " conflicts" : "" });
      if (a.value === "cancel") return setMsg($("pr-status"), "Import cancelled - nothing was changed.");
      action = a.value;
      if (a.all) remembered[c.with] = action;
    }
    resolutions[c.id] = action;
  }
  try {
    const r = await api("/api/prompts/import", { method: "POST", body: { data, mode, resolutions } });
    await loadPrompts();
    const parts = [r.added.length && `${r.added.length} added`, r.replaced.length && `${r.replaced.length} overwritten`,
      r.renamed.length && `${r.renamed.length} renamed (${r.renamed.map(([a, b]) => `${a} -> ${b}`).join(", ")})`, r.skipped.length && `${r.skipped.length} skipped`].filter(Boolean);
    setMsg($("pr-status"), `Imported from ${file.name}: ${parts.join(", ") || "nothing"}.` + (r.problems.length ? "\nNot imported: " + r.problems.join("; ") : ""), r.problems.length ? "" : "ok");
  } catch (e) { fail("Import failed: " + e.message); }
}

// built-in prompts are read-only: fields greyed, Save and Delete disabled; "Copy to new prompt" turns one into an
// editable custom prompt (not possible for a code-based prompt - a coding session is generated in code and cannot be
// reproduced in the form). A custom prompt is fully editable; Delete stays enabled for it, even one that carries a
// session via hand-edited JSON. p = null: a new, empty prompt.
function setPromptEditorLock(p) {
  const generated = !!p && !!p.adaptive, codeBased = !!p && (isCodeBased(p) || generated), locked = !!p && (p.builtin || codeBased);
  ["pr-id", "pr-name", "pr-cat", "pr-text", "pr-expected"].forEach((id) => {
    $(id).readOnly = locked; $(id).classList.toggle("pedit-lock", locked);
  });
  ["pr-lang", "pr-difficulty", "pr-check", "pr-save", "pr-img-add"].forEach((id) => {
    $(id).disabled = locked; $(id).classList.toggle("pedit-lock", locked);
  });
  $("pr-del").disabled = !!p && p.builtin;
  $("pr-copy").disabled = !(p && p.builtin && !codeBased);
  $("pr-status").textContent = generated
    ? "Generated prompt: written when a run starts, sized to the context the model is served with - it cannot be edited or copied here."
    : codeBased
    ? "Code-based prompt (coding session): generated in code, so it cannot be edited or copied here."
    : locked ? 'Built-in prompt (read-only). Use "Copy to new prompt" to build your own question from it.'
    : "";
}
$("pr-copy").addEventListener("click", () => {
  const p = state.prompts.find((x) => x.id === state.promptBase);
  if (!p || !p.builtin) return;
  const taken = new Set(state.prompts.map((x) => x.id));
  let id = p.id + "_copy", n = 2;
  while (taken.has(id)) id = `${p.id}_copy${n++}`;
  $("pr-id").value = id; $("pr-name").value = p.name + " (copy)"; $("pr-cat").value = "custom";
  setPromptEditorLock(null);  // state.promptBase stays: the server takes tools and turns from the original
  setMsg($("pr-status"), `Copy of '${p.id}': give it a new ID, change what you need and save.`);
  $("pr-id").focus(); $("pr-id").select();
});
bindSort($("pr-table"), () => state.promptSort || { key: null, desc: false },
  (s) => { state.promptSort = s; store("prompt-sort", JSON.stringify(s)); }, renderPromptTable);
$("pr-new").addEventListener("click", () => {
  $("pr-id").value = ""; $("pr-name").value = ""; $("pr-cat").value = "custom"; $("pr-lang").value = ""; $("pr-difficulty").value = "easy"; $("pr-text").value = ""; $("pr-expected").value = "";
  $("pr-check").value = ""; $("pr-extra").textContent = ""; state.promptBase = null;
  state.promptImages = []; renderPromptImages();
  setPromptEditorLock(null);
  $("pr-id").focus();
});
$("pr-save").addEventListener("click", async () => {
  const id = $("pr-id").value.trim();
  try {
    await api("/api/prompts", { method: "POST", body: { id, name: $("pr-name").value, category: $("pr-cat").value, lang: $("pr-lang").value, difficulty: $("pr-difficulty").value, text: $("pr-text").value,
      expected: $("pr-expected").value, check: $("pr-check").value, base: state.promptBase, images: state.promptImages } });
    await loadPrompts();
    setMsg($("pr-status"), `Prompt '${id}' saved.`, "ok");
  } catch (e) { setMsg($("pr-status"), e.message, "err"); }
});
$("pr-del").addEventListener("click", async () => {
  const id = $("pr-id").value.trim();
  if (!id || !confirm(`Delete prompt '${id}'?`)) return;
  try {
    await api("/api/prompts/" + encodeURIComponent(id), { method: "DELETE" });
    await loadPrompts();
    setMsg($("pr-status"), `Prompt '${id}' deleted.`);
  } catch (e) { setMsg($("pr-status"), e.message, "err"); }
});

