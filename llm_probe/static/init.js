"use strict";
// start - part of the page, loaded in the order of the <script> tags in index.html
// --------------------------------------------------------------------- init
// printable reports (?report=<run id>, ?compare=<run ids>): opened by "Export PDF", saved as PDF by the browser
async function showReport(title, subtitle, body, after) {
  document.title = title.replace(/[\\/:*?"<>|]/g, "_");
  document.querySelector("main").innerHTML =
    '<div class="report-bar no-print">' + esc(subtitle) + ' - <button class="btn primary" onclick="window.print()">' +
    'Print / save as PDF</button> In the print dialog choose <b>Save as PDF</b> as the printer.</div>' +
    `<div class="report-head"><h1>LLM Probe</h1><div class="tagline">Speed, accuracy and capabilities of LLM endpoints</div>` +
    `<div class="dim">${esc(subtitle)} · created ${new Date().toLocaleString()}</div></div><div class="card">${body}</div>`;
  if (after) after();
  await document.fonts.ready;
  setTimeout(() => window.print(), 400);
}
async function runReport(id) {
  const d = await api("/api/runs/" + id), r = d.run;
  await showReport(`LLM Probe - run ${r.id} - ${r.model}`,
    `Run report · run #${r.id} · ${r.endpoint} · ${r.model} · started ${r.started.replace("T", " ")} · status ${r.status}` +
    (r.settings.version ? " · LLM Probe v" + r.settings.version : "") +
    (r.settings.promptset ? " · prompt set v" + r.settings.promptset : "") +
    (r.note ? " · note: " + r.note : ""), renderRun(d));
}
async function compareReport(ids) {
  const c = await api("/api/compare?runs=" + ids);
  const runs = compareRuns(c);
  await showReport(`LLM Probe - comparison ${runs.map((r) => "#" + r.d.run.id).join(" ")}`,
    `Comparison · ${runs.map((r) => `${r.L} = run #${r.d.run.id} ${r.d.run.model}`).join(" · ")}`, compareHtml(c, runs), () => {
      markRunCells(hlRoot());
      setPin(+new URLSearchParams(location.search).get("pin") || null);  // the run pinned in the Compare tab is printed highlighted
    });
}

(async () => {
  busyOn();  // the first load: shown until the tabs are filled
  const q = new URLSearchParams(location.search);
  if (q.get("report") || q.get("compare")) {
    state.report = true;
    state.reportHosts = q.get("hosts") === "1";
    document.body.classList.add("report");
    setPrompts(await api("/api/prompts"));
    return q.get("report") ? runReport(q.get("report")) : compareReport(q.get("compare"));
  }
  applyTips();
  await loadPrompts();
  await loadConfig();
  fillSettings(state.cfg.bench);
  editEndpoint(state.cfg.endpoints.length ? state.cfg.endpoints[0].name : "");
  await loadRuns();
  showTab(recallTab() || "home");
  busyOff();
  pollBench();
})();
