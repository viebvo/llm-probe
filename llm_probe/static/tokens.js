"use strict";
// token consumption - part of the page, loaded in the order of the <script> tags in index.html
// ---------------------------------------------------------------- token consumption
const PHASES = { single: "Single requests", load: "Concurrent load", sweep: "Context sweep", session: "Long sessions", total: "Total" };
// " EUR" for the costs of a run priced in a currency, else ""
const currencyUnit = (price) => (price && price.currency ? " " + esc(price.currency) : "");
function showPriceUnit() {
  const unit = $("ep-currency").value.trim();
  document.querySelectorAll(".unitwrap .unit").forEach((u) => { u.textContent = unit; });
}
$("ep-currency").addEventListener("input", showPriceUnit);
function tokenSection(key, runs) {
  const one = runs.length === 1, rows = [];
  const tok = (v) => fmt(v, 0);
  const priced = runs.some((x) => x.d.tokens.price);
  // phase-major: the phase is the group (first column, shaded), the runs within a phase share the shade
  const byRun = runs.map((r) => Object.fromEntries(
    [...r.d.tokens.phases, ...(r.d.tokens.phases.length > 1 ? [r.d.tokens.total] : [])].map((ph) => [ph.phase, ph])));
  const order = ["single", "load", "session", "total"].filter((ph) => byRun.some((m) => m[ph]));
  for (const phase of order) {
    let first = true;
    runs.forEach((r, ri) => {
      const ph = byRun[ri][phase];
      if (!ph) return;
      const est = ph.estimated ? `<span class="yellow" title="The server sent no token counts for these requests: output tokens were counted from stream chunks.">${ph.estimated}/${ph.requests} estimated</span>`
        : '<span class="green">from server</span>';
      const reason = ph.reasoning == null ? '<span class="dim" title="Responses of the load phase are not stored">-</span>'
        : `${tok(ph.reasoning)} <small class="dim">(${fmt((ph.reasoning_share || 0) * 100, 0, " %")})</small>`;
      rows.push({ cls: phase === "total" ? "bold" : "", group: phase,
        cells: [first ? esc(PHASES[phase] || phase) : "", ...(one ? [] : [`<span class="c${r.i}"${first ? ' style="font-weight:600"' : ""}${runTip(r)}>${esc(r.L || r.name)}</span>`]),
          ph.requests, tok(ph.input), tok(ph.output), reason, fmt(ph.per_request, 0), est,
          ...(priced ? [ph.cost == null ? "-" : fmt(ph.cost, 4)] : [])] });
      first = false;
    });
  }
  const head = ["Phase", ...(one ? [] : ["Run"]), "Requests", "Input tok.", "Output tok.", "of which reasoning (est.)",
    "Output tok./request", "Token counts", ...(priced ? ["Cost" + (new Set(runs.map((x) => (x.d.tokens.price || {}).currency || "")).size === 1 ? currencyUnit(runs[0].d.tokens.price).replace(/^ (.+)$/, " ($1)") : "")] : [])];
  const facts = runs.map((r) => {
    const u = r.d.tokens, parts = [];
    if (u.per_correct) parts.push(`${fmt(u.per_correct, 0)} output tokens per correct answer`);
    if (u.image_input) parts.push(`prompts with an image: ${fmt(u.image_input, 0)} input tokens on average`);
    if (u.price) parts.push(`price per 1M tokens: ${fmt(u.price.in, 2)} in / ${fmt(u.price.out, 2)} out${esc(currencyUnit(u.price))}`);
    return parts.length ? `<div class="dim">${one ? "" : `<b class="c${r.i}"${runTip(r)}>${esc(r.L || r.name)}</b>: `}${parts.join(" · ")}</div>` : "";
  }).join("");
  return section(key, "Token usage", '<p class="hint">Tokens of all successful requests. Reasoning is estimated from ' +
    "the share of the response text that is reasoning (responses of the load phase are not stored). Where the server " +
    "reports no token counts, output tokens are counted from stream chunks - marked as estimated.</p>" + facts + table("", head, rows));
}

