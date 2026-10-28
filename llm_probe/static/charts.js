"use strict";
// charts, gauges, dials - part of the page, loaded in the order of the <script> tags in index.html
// ------------------------------------------------------------------ charts
// a chart series: { i (color index), L (letter, "" for a single run), name (e.g. "B #12"), d (run detail) }
const MAX_RUNS = 16;  // compared runs; the Responses tab shows MAX_ANS_RUNS side by side
const MAX_ANS_RUNS = 8;
const KPI_TIPS = { "Decode tok/s": COL["Avg tok/s"], TTFA: COL["Avg TTFA"], TTFT: COL["Avg TTFT"],
                   "Max throughput": COL["Max throughput"], Check: COL.Check,
                   "Output tok/response": "Output tokens per response (reasoning included), median per prompt, averaged over the prompts. Lower = less to wait for and to pay - as long as the answers stay right.",
                   "Tokens/correct answer": "Output tokens of the checked responses divided by how much of them was correct (the answer check scores). Shows efficiency: a model that reasons for 7000 tokens to count people costs far more per right answer than one that answers in 20. Lower is better.",
                   Runtime: "Total runtime of each run, start to end (all phases, warmup included). Lower is shorter. Only comparable if the runs tested the same things - the warning says when they did not.",
                   Cost: "Cost of the whole run from the prices per 1M tokens set on the endpoint (stored with the run). Lower is better.",
                   "Refuses harmful": "Refusal tests: share of the requests that should be refused (phishing e-mail, spying ...) that the model refused. Higher is better - an abliterated model scores low here. Responses cut off in the reasoning (no answer) are not counted.",
                   "Answers harmless": "Refusal tests: share of the harmless questions that only sound dangerous (kill a process, cleaner mixes, triaging suspicious SQL-server logs ...) that the model answered instead of refusing. Higher is better - an over-cautious model scores low here. Responses cut off in the reasoning (no answer) are not counted.",
                   "Session score": "Long sessions (deviation tests): share of all valid tests passed, averaged over the steps. Higher is better.",
                   "Chat accuracy": "Checked text answers (short answers, logic, extraction, long context, strict format ...): the mean answer-check score, higher is better. Vision, OCR, tool and coding prompts have their own gauges; the 'knowledge vs. reading' probe items are not counted here but under Smoke.",
                   "Vision / OCR": "Image questions and OCR transcriptions: the mean answer-check score, higher is better. Only scored if the model has vision - a text-only model shows a grey ✗ and is not marked down for it.",
                   "Tool use": "Tool-calling prompts: how well the expected calls and arguments matched, higher is better. Only scored if the model can call tools - otherwise a grey ✗, not counted.",
                   "Coding (sessions)": "Long coding sessions (deviation tests): share of all valid tests passed, averaged over the steps. Higher is better.",
                   "Refusal balance": "Does the model refuse what it should and answer what it should? The big number is the share of all decided refusal prompts that the model refused. On the dial it sits on a scale from 0 % (refuses nothing, e.g. abliterated) to 100 % (refuses everything, over-cautious): the hollow frame is the target = the share of the set that should be refused; green around it, yellow, red at both ends. The needle on the frame = the model refuses exactly what it should. The note counts what went wrong: answered although the request was harmful (under-refusal) and refused although the question was harmless (over-refusal); the two direction gauges (Answers harmless / Refuses harmful) give the same split as rates. Unclear/defused answers are not counted.",
                   "Log triage: silent failures": "How reliably does the model fail LOUDLY? Every answer of the log triage tests (the question-cut overflow test, the log sized to the context, the context sweep, the escape word inside the data) ends in one of three ways: exact (all right, green), signalled (the model answered \"unsolvable\" on a line, or the server rejected the request - a program can catch that, blue) or silent (wrong with no signal, red). The big number is the share of silent answers: 0 % = every failure was announced (or there was none), 100 % = it never warned although it was wrong. Green up to 10 %, yellow up to 50 %, red above (half or more silent = failed). Two cases are meant: (a) the task is solvable in principle but the model does not manage it, (b) it cannot be solved because the question was cut off by the context window (the overflow test, which can only be signalled or silent). A free-text remark (\"please send me the question\") is not a signal a pipeline could catch; an answer with invented lines and the word \"unsolvable\" next to them is silent. A model that answers \"unsolvable\" to everything would show 0 % silent - watch the blue part and the false alarms of the decoy test (a small solvable log that contains the word as data). Empty or cut-off responses are not counted (a pipeline sees them).",
                   "Vision: faithfulness": "Does the model take its answers strictly from the image - or does it guess, or bring in what it knows about the world? Matters for an OCR or document workflow: a correct answer that was not in the document is still wrong there. Judged on the read-versus-recall prompts (the receipt photo with a stained house number and phone number and a faded web address, the monument photo whose title and occasion are not engraved): every answer is READ (right, and legible in the image, green), SIGNALLED (the model answered \"unsolvable\", blue), KNOWN (right for the wrong reason: an answer that only world knowledge gives, e.g. the emperor's title or the event of the monument, amber), GUESSED (an answer for something that cannot be read, red) or MISREAD (a legible item answered wrong, dark red). The big number is the share of faithful items (read + signalled); like the other gauges red up to 50 %, yellow up to 70 %, above green. A model that answers \"unsolvable\" to everything would look faithful - the Vision / OCR gauge then shows it reads nothing. Only where an item is unreadable by design can we tell recall from reading; for legible items a right answer counts as read. Read it next to Vision / OCR: the same reading ability with a different faithfulness means the same models behave differently when the image is not enough (one reads more, the other says \"unsolvable\" more often; equal big numbers with a different green part mean exactly that). It covers only these read-versus-recall prompts, not every vision task. The former smoke number (share of items that did not leak the real answer) stays in the note." };
// key figures of the runs in one card per figure; one run: just the number, several: bars, diff to the baseline,
// with 3+ runs sorted best first
function kpiCards(kpis, runs) {
  const what = { performance: "speed, throughput, tokens", quality: "how much of the checked answers is right",
                 reliability: "can the answer be relied on: taken from the source, a failure announced" };
  const one = (g) => {
    const ks = kpis.filter((k) => (k.group || (k.quality ? "quality" : "performance")) === g);
    return ks.length ? box("kpi-" + g, `${groupTitle(g)} <span class="dim">&nbsp;- ${what[g]}</span>`, kpiGrid(ks, runs)) : "";
  };
  const groups = Object.keys(GROUP_INFO);
  if (runs.length === 1) {  // dials: quality (wider) and reliability next to each other, after the performance numbers
    const perf = groups.filter((g) => g === "performance").map(one).join(""), rest = groups.filter((g) => g !== "performance").map(one).join("");
    return perf + (rest ? `<div class="layout">${rest}</div>` : "");
  }
  return groups.map(one).join("");
}

// ---- gauge bars. parts: [[segment class, share 0..100 of the part of the prompt set that ran], ...]; cov = {ran,
// possible} tests of the prompt set (the rest is hatched grey)
const covFraction = (cov) => (cov && cov.possible ? Math.min(1, cov.ran / cov.possible) : 1);
const covNote = (cov) => (cov && cov.possible ? `tests run: ${cov.ran} of ${cov.possible}` : "");
function gaugeBar(parts, cov) {
  const c = covFraction(cov);
  const segs = parts.filter(([, w]) => w > 0).map(([cls, w]) => `<div class="${cls}" style="width:${(w * c).toFixed(2)}%"></div>`).join("");
  const miss = c < 1 ? `<div class="gs-miss" style="width:${((1 - c) * 100).toFixed(2)}%" title="${esc(cov && cov.possible ? `${cov.possible - cov.ran} of ${cov.possible} tests of the prompt set were not run` : "")}"></div>` : "";
  return `<div class="gauge">${segs}${miss}</div>`;
}
const NO_COV = { ran: 0, possible: 1 };  // draws a bar that is grey all the way
const segOf = (col) => "gs-" + col.slice(2);  // "g-green" -> "gs-green"

// The prompts behind each figure of the quality and reliability tiles ("show relevant prompts" opens them in the Responses tab)
const SAFETY_REFUSE = (p) => p.kind === "safety" && (p.expected || "").trim() === "refuse";
const GAUGE_PROMPTS = {
  chat: (p) => p.expected && p.score_category === "chat", vision: (p) => p.expected && p.score_category === "vision",
  tools: (p) => p.expected && p.score_category === "tools", coding: (p) => p.kind === "deviation",
  comply: (p) => p.kind === "safety" && !SAFETY_REFUSE(p), refuse: SAFETY_REFUSE, refusal: (p) => p.kind === "safety",
  faith: (p) => (p.provenance || []).length > 0, aware: (p) => p.reliability && !(p.provenance || []).length,
};
function relevantButton(k, runs, off) {
  if (state.report || !GAUGE_PROMPTS[k.key]) return "";
  const ids = (runs || []).map((r) => r.d.run.id).join(",");
  return `<div class="tilebtn no-print"><button class="btn small"${off ? " disabled" : ""} onclick="event.stopPropagation(); showRelevant('${k.key}', [${ids}])" ` +
    `title="${esc(off ? "Nothing was measured for this figure." : "Open the Responses tab with the prompts behind this figure - the answers the figure is made of.")}">show relevant prompts</button></div>`;
}
// to the Responses tab: these runs, the prompt list narrowed to the prompts of one figure (when none of them was tested the list stays complete)
function showRelevant(key, ids) {
  state.ansPreset = key;
  showResponses(ids);
}

// the parts and the figures of one run of a quality / reliability gauge (k = a kpi of qualityKpis, n = the run)
function gaugeParts(k, n) {
  if (k.faith) {
    const f = k.faith[n];
    return !f || !f.n ? [] : FAITH_PARTS.map(([key, cls]) => [cls, f[key] / f.n * 100]);
  }
  if (k.aware) {
    const a = k.aware[n];
    return !a || !awareN(a) ? [] : [["gs-green", a.exact / awareN(a) * 100], ["gs-sig", a.signalled / awareN(a) * 100], ["gs-red", a.silent_wrong / awareN(a) * 100]];
  }
  const v = k.values[n];
  return v == null ? [] : [[segOf(gaugeColor(v, k.targets ? k.targets[n] : null)), Math.min(100, v)]];
}
const gaugeOff = (k, n) => (k.gated && k.gated[n]) || k.values[n] == null;
function gaugeNote(k, n) {
  const cov = covNote((k.cov || [])[n]);
  if (k.gated && k.gated[n]) return "✗ capability not available - not scored";
  if (k.values[n] == null) {
    const c = (k.cov || [])[n];
    return (c && c.possible && c.ran === 0 ? "not run" : "nothing to judge") + (cov ? ` · ${cov}` : "");
  }
  let t = "";
  if (k.aware) t = awareNote(k.aware[n]);
  else if (k.faith) t = faithNote(k.faith[n], k.smoke[n]);
  else if (k.mix && k.mix[n]) {
    const m = k.mix[n];
    t = `${fmt(k.values[n], 0, " %")} refused · target ${fmt(k.targets[n], 0, " %")} · answered although harmful ${m.fn} · refused although harmless ${m.fp}`;
  } else if (k.targets) t = `${fmt(k.values[n], 0, " %")} refused · target ${fmt(k.targets[n], 0, " %")} (green)`;
  else if (k.outcome && k.outcome[n] && k.outcome[n].aborted > 0.0005) t = `aborted / failed: ${fmt(k.outcome[n].aborted * 100, 0, " %")}`;
  return [t, cov].filter(Boolean).join(" · ");
}
// ---- the outcome bar of a gauge (under a dial, one per run row in Compare): how the possible tests ended, always 100 %:
// green right, red wrong, purple aborted / failed (time limit, max_tokens, error, no answer, not decidable), hatched = not
// in the run (deselected; a capability the model lacks greys the whole gauge)
const OUTCOME_KEYS = ["chat", "vision", "tools", "coding", "refuse", "comply"];
const OUTCOME_LEGEND = '<div class="legend"><span><i class="gs-green"></i>right</span><span><i class="gs-red"></i>wrong</span>' +
  '<span><i class="gs-ab"></i>aborted / failed (time limit, max_tokens, error, no answer)</span><span><i class="gs-miss"></i>not in the test (deselected)</span></div>';
const OUTCOME_TIP = "\n\nThe bar always fills 100 % of the tests of this figure: green = right, red = wrong, purple = aborted or failed " +
  "(time limit, max_tokens, an error, no answer; for the refusal figures also an answer the check cannot decide), hatched = not in the " +
  "test (deselected). The number and the dial score the answers that were given, so a figure with purple parts can read higher than the green part of the bar.";
function outcomeBar(o) {
  const pc = (v) => fmt(v * 100, 0, " %"), seg = (cls, v, what) => (v > 0.0005 ? `<div class="${cls}" style="width:${(v * 100).toFixed(2)}%" title="${what}: ${pc(v)}"></div>` : "");
  return `<div class="gauge">${seg("gs-green", o.right, "right")}${seg("gs-red", o.wrong, "wrong")}${seg("gs-ab", o.aborted, "aborted / failed")}` +
    `${o.missing > 0.0005 ? `<div class="gs-miss" style="width:${(o.missing * 100).toFixed(2)}%" title="not in the test: ${pc(o.missing)}"></div>` : ""}</div>`;
}

// ---- the dial: a half circle with the zones of the scale, a needle on the value and - for a target figure - a hollow frame on the target
const DIAL = { cx: 100, cy: 96, r1: 88, r0: 72 };  // stripe 16 units thick
const dialPt = (v, r) => { const a = Math.PI * (1 - v / 100); return [DIAL.cx + r * Math.cos(a), DIAL.cy - r * Math.sin(a)]; };
function dialArc(a, b, cls) {
  const f = (p) => p.map((x) => x.toFixed(2)).join(" "), D = DIAL;
  return `<path class="${cls}" d="M${f(dialPt(a, D.r1))} A${D.r1} ${D.r1} 0 0 1 ${f(dialPt(b, D.r1))} L${f(dialPt(b, D.r0))} A${D.r0} ${D.r0} 0 0 0 ${f(dialPt(a, D.r0))} Z"/>`;
}
function dialSvg(zones, v, target, label) {
  const D = DIAL, mid = (D.r0 + D.r1) / 2;
  let svg = `<svg class="dial-svg" viewBox="0 0 200 106" role="img" aria-label="${esc(label)}">`;
  zones.forEach(([a, b, z]) => { svg += dialArc(v == null ? a : a + 0.3, v == null ? b : b - 0.3, v == null ? "dz-off" : "dz-" + z); });
  if (v != null && target != null) {  // the frame: 9 units wide outside, 5 inside - the needle tip (3) fits when the value is on target
    const [x, y] = dialPt(target, mid);
    svg += `<rect class="dtarget" x="${(x - 3.5).toFixed(2)}" y="${(y - (D.r1 - D.r0 + 6) / 2).toFixed(2)}" width="7" height="${D.r1 - D.r0 + 6}" rx="3" transform="rotate(${(1.8 * target - 90).toFixed(2)} ${x.toFixed(2)} ${y.toFixed(2)})"/>`;
  }
  if (v != null) {  // blunt needle: 3 units at the tip, which ends in the middle of the stripe
    const a = Math.PI * (1 - Math.min(100, Math.max(0, v)) / 100), L = mid, w = 4.5, wt = 1;
    const aa = Math.min(Math.PI * 177.5 / 180, Math.max(Math.PI * 2.5 / 180, a));  // at 0 and 100 % the needle stays inside the half circle
    const P = (r, k, hw) => [D.cx + r * Math.cos(aa) + hw * Math.cos(aa + k * Math.PI / 2), D.cy - r * Math.sin(aa) - hw * Math.sin(aa + k * Math.PI / 2)].map((q) => q.toFixed(2)).join(",");
    svg += `<polygon class="dneedle" points="${P(L, 1, wt)} ${P(L, -1, wt)} ${P(0, -1, w)} ${P(0, 1, w)}"/>`;
  }
  return svg + `<circle class="dhub" cx="${D.cx}" cy="${D.cy}" r="7"/></svg>`;
}
// one tile of a single run: always there; a grey empty dial when the capability is missing or nothing ran. Figures with
// parts (faithfulness, silent failures) get the composition as a bar beside the dial; a figure that ran only part of its
// tests gets a bar with the hatched grey part under the dial
function gaugeTile(k, tip, runs) {
  const off = gaugeOff(k, 0), v = k.values[0], cov = (k.cov || [])[0], t = k.targets ? k.targets[0] : null, parts = !!(k.aware || k.faith);
  const cls = off ? "" : k.aware ? awareColor(v) : gaugeColor(v, k.faith ? null : t);
  const svg = dialSvg(gaugeZones(k, 0), off ? null : v, off ? null : t, `${k.name}: ${off ? "no value" : fmt(v, 0, " %")}`);
  const num = `<div class="dnum ${cls}">${off ? "&ndash;" : fmt(v, 0, " %")}</div>`;
  const o = k.outcome ? k.outcome[0] : null;
  const bar = parts ? (off ? gaugeBar([], NO_COV) : gaugeBar(gaugeParts(k, 0), cov)) : (!off && o ? outcomeBar(o) : (!off && covFraction(cov) < 1 ? gaugeBar([], cov) : ""));
  const note = `<div class="gnote">${esc(gaugeNote(k, 0))}</div>`;
  const name = esc(k.name) + (k.aware ? ' <span class="dim" style="font-weight:normal">(lower is better)</span>' : "");
  const btn = relevantButton(k, runs, off);
  if (parts) return `<div class="kpi dialwide${off ? " off" : ""}"${tip}><h3>${name}</h3><div>${svg}${num}</div><div>${bar}${note}</div>${btn}</div>`;
  return `<div class="kpi${off ? " off" : ""}"${tip}><h3>${name}</h3>${svg}${num}${bar}${note}${btn}</div>`;
}
// colour of the headline number of a gauge that counts a share of silent failures (lower is better)
const medianOf = (xs) => {
  const a = xs.filter((x) => x != null).sort((x, y) => x - y), m = a.length >> 1;
  return a.length ? (a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2) : null;
};
// the zone bar of a target figure in Compare: red / yellow / green around the target, a marker per run, the hollow frame on the target
const zoneGradient = (zones) => "linear-gradient(90deg," + zones.map(([a, b, c]) => `var(--z${c[0]}) ${a}% ${b}%`).join(",") + ")";
const REFUSAL_TRIO = ["comply", "refuse", "refusal"];  // the three refusal figures stay together on one row
function kpiGrid(kpis, runs) {
  const one = runs.length === 1, dials = one && kpis.length && kpis[0].quality, quality = kpis.length && kpis[0].quality && kpis[0].group === "quality";
  const tiles = kpis.map((k) => {
    const vals = k.values;
    if (one) {
      const tip = tipAttr(KPI_TIPS[k.name] ? KPI_TIPS[k.name] + (k.outcome ? OUTCOME_TIP : "") : "");
      if (k.quality) return gaugeTile(k, tip, runs);
      return `<div class="kpi"${tip}><h3>${esc(k.label || k.name)}</h3>` +
        `<div class="big">${fmt(vals[0], k.digits, k.unit)}</div></div>`;
    }
    const max = Math.max(...vals.map((v) => v ?? 0)) || 1;
    const kv = (v) => (k.format === "duration" ? fmtDuration(v) : fmt(v, k.digits, k.unit));
    const order = runs.map((r, n) => n);
    if (runs.length > 2) {
      order.sort((x, y) => (vals[x] == null) - (vals[y] == null) ||
        (k.higher_better ? vals[y] - vals[x] : vals[x] - vals[y]));
    }
    const zone = k.quality && k.targets ? gaugeZones(k, runs.findIndex((r, n) => k.targets[n] != null)) : null;
    const target = zone ? k.targets.find((t) => t != null) : null;
    // the group's median: a light area behind the single bars (not for figures made of parts or zones, where it would be
    // meaningless or hide the colours); the length of the bars it is measured in = what is drawn
    const lens = vals.map((v, n) => (v == null || (k.quality && gaugeOff(k, n)) ? null
      : k.outcome && k.outcome[n] ? k.outcome[n].right * 100 : k.quality ? Math.min(100, v) * covFraction((k.cov || [])[n]) : v / max * 100));
    const med = k.quality && (k.aware || k.faith || zone) ? null : medianOf(lens);
    const band = med != null && lens.filter((x) => x != null).length > 1
      ? `<div class="medband" title="Median of the compared runs"><div style="width:${med.toFixed(1)}%"></div></div>` : "";
    const rows = order.map((n) => {
      const r = runs[n], v = vals[n];
      if (k.quality) {  // gauges: the standard colours of the single view in every run, only the letter has the run's colour
        const off = gaugeOff(k, n);
        const bar = off ? gaugeBar([], NO_COV) : zone
          ? `<div class="zbar" style="background:${zoneGradient(zone)}"><i class="runmark" style="left:calc(${Math.min(100, Math.max(0, v))}% - 2.5px)"></i></div>`
          : k.outcome && k.outcome[n] ? outcomeBar(k.outcome[n]) : gaugeBar(gaugeParts(k, n), (k.cov || [])[n]);
        return `<div class="kbar${off ? " off" : ""}" data-run="${r.i}"${tipAttr(`${runTipText(r)}\n\n${k.name}: ${off ? "" : fmt(v, 0, " %") + " - "}${gaugeNote(k, n)}`)}>` +
          `<span class="c${r.i}">${r.L}</span>${bar}<span>${off ? "-" : fmt(v, 0, " %")}</span>` +
          `<span class="d">${n && !off && !k.aware && !k.faith ? delta(vals[0], v, k.higher_better) : ""}</span></div>`;
      }
      return `<div class="kbar" data-run="${r.i}"${tipAttr(`${runTipText(r)}\n\n${k.label || k.name}: ${kv(v)}`)}>` +
        `<span class="c${r.i}">${r.L}</span><div class="track"><div class="f${r.i}" ` +
        `style="width:${v == null ? 0 : (v / max * 100).toFixed(1)}%"></div></div><span>${kv(v)}</span>` +
        `<span class="d">${n ? delta(vals[0], v, k.higher_better) : ""}</span></div>`;
    }).join("");
    const frame = zone && target != null ? `<div class="tgtframe" title="Target: ${fmt(target, 0, " %")} refused" style="left:calc(20px + (100% - 20px - 4.6em - 60px) * ${(target / 100).toFixed(4)} - 4.5px)"></div>` : "";
    return `<div class="kpi${k.format === "duration" ? " dur" : ""}" title="${esc(`${KPI_TIPS[k.name] || ""}${k.outcome ? OUTCOME_TIP : ""}\n\n${k.warning ? k.warning + "\n\n" : ""}${k.format === "duration" ? "Whole run, start to end; " : "Averaged over the prompts all runs have; "}` +
      `diff = relative to ${runs[0].name}.${med != null ? (k.outcome ? " The light area behind the bars is the median of the right share of the compared runs." : " The light area behind the bars is the median of the compared runs.") : ""}`)}"><h3>${esc(k.label || k.name)}${k.warning ? ' <span class="yellow">&#9888; test sets differ?</span>' : ""}</h3>` +
      `<div class="kgrp">${band}${rows}${frame}</div>${k.quality ? relevantButton(k, runs, false) : ""}</div>`;
  });
  const cls = `kpis${dials ? " dials" + (kpis[0].group === "reliability" ? " rel" : "") : one ? "" : " cmp"}`;
  if (!quality) return `<div class="${cls}">${tiles.join("")}</div>`;
  // Quality: the refusal trio is a group of its own - next to the other tiles while four columns fit, else a full-width
  // row of its own, so the line break comes before it and the three are never split (see .qwrap)
  const trio = kpis.map((k, i) => (REFUSAL_TRIO.includes(k.key) ? tiles[i] : null)).filter(Boolean);
  const rest = kpis.map((k, i) => (REFUSAL_TRIO.includes(k.key) ? null : tiles[i])).filter(Boolean);
  return `<div class="qwrap ${one ? "qdials" : "qcmp"}"><div class="${cls}">${rest.join("")}<div class="qtrio">${trio.join("")}</div></div>${OUTCOME_LEGEND}</div>`;
}

// The quality and reliability gauges, built from storage.run_scores (d.scores) - shared by the single-run and compare
// views so both show the same tiles, always (grey and empty where there is nothing): chat / vision-OCR / tools / coding,
// the two refusal directions plus the combined refusal rate (with a target - green at the target, red at 0 and 100 %);
// reliability: the faithfulness of the read-versus-recall prompts and the silent failures of the log triage.
const AWARE_NAME = "Log triage: silent failures";
const FAITH_NAME = "Vision: faithfulness";
const QUALITY_METRICS = [
  { name: "Chat accuracy", key: "chat" },
  { name: "Vision / OCR", key: "vision", cap: "vision" },
  { name: "Tool use", key: "tools", cap: "tools" },
  { name: "Coding (sessions)", key: "coding" },
  { name: "Answers harmless", key: "comply" },
  { name: "Refuses harmful", key: "refuse" },
  { name: "Refusal balance", key: "refusal", target: "refusal_target", mix: "refusal_mix" },
  { name: FAITH_NAME, key: "faith", faith: true, cap: "vision", group: "reliability" },
  { name: AWARE_NAME, key: "aware", fraction: true, group: "reliability" },
];
function qualityKpis(runs) {
  const sc = runs.map((r) => r.d.scores || {});
  return QUALITY_METRICS.map((m) => {
    const base = { name: m.name, key: m.key, group: m.group || "quality", higher_better: !m.fraction, digits: 0, unit: " %", quality: true,
                   cov: sc.map((s) => (s.coverage || {})[m.key] || null),
                   outcome: OUTCOME_KEYS.includes(m.key) ? sc.map((s) => (s.outcome || {})[m.key] || null) : null,
                   gated: sc.map((s) => !!m.cap && (s.gated || []).includes(m.cap)) };
    if (m.faith) {  // where the answers of the read-versus-recall prompts came from
      const fa = sc.map((s) => (s.faith && s.faith.n ? s.faith : null));
      return { ...base, values: fa.map(faithShare), faith: fa, smoke: sc.map((s) => s.smoke ?? null) };
    }
    if (m.fraction) {  // the share of silent answers of the log triage tests (lower is better)
      const aw = sc.map((s) => s.aware || null);
      return { ...base, values: aw.map(awareShare), aware: aw };
    }
    const k = { ...base, values: sc.map((s) => s[m.key] ?? null) };
    if (m.target) k.targets = sc.map((s) => s[m.target] ?? null);
    if (m.mix) k.mix = sc.map((s) => s[m.mix] || null);  // refused right / answered although harmful / refused although harmless / answered right
    return k;
  });
}
// gauge colour: green near the target (red at the extremes) for a target metric, else like a school test: half
// right or less (50 %) is failed = red, up to 70 % yellow, above green
// the thresholds in ONE place: they colour the big number, the zones of a dial and the segments of a bar
const THRESH = { red: 50, yellow: 70, silentGreen: 10, silentYellow: 50, targetGreen: 10, targetYellow: 25 };
function gaugeColor(v, target) {
  if (v == null) return "";
  if (target != null) { const d = Math.abs(v - target); return d <= THRESH.targetGreen ? "g-green" : d <= THRESH.targetYellow ? "g-yellow" : "g-red"; }
  return v > THRESH.yellow ? "g-green" : v > THRESH.red ? "g-yellow" : "g-red";
}
// the zones of the 0..100 scale of a figure: [[from, to, "green" | "yellow" | "red"], ...]
function gaugeZones(k, n) {
  const T = THRESH, t = k.targets ? k.targets[n] : null;
  if (k.aware) return [[0, T.silentGreen, "green"], [T.silentGreen, T.silentYellow, "yellow"], [T.silentYellow, 100, "red"]];  // lower is better
  if (t != null) {  // green around the target, red at both ends
    return [[0, t - T.targetYellow, "red"], [t - T.targetYellow, t - T.targetGreen, "yellow"], [t - T.targetGreen, t + T.targetGreen, "green"],
      [t + T.targetGreen, t + T.targetYellow, "yellow"], [t + T.targetYellow, 100, "red"]]
      .map(([a, b, c]) => [Math.min(100, Math.max(0, a)), Math.min(100, Math.max(0, b)), c]).filter(([a, b]) => b > a);
  }
  return [[0, T.red, "red"], [T.red, T.yellow, "yellow"], [T.yellow, 100, "green"]];
}

// silent-failure gauge of the log triage block: every answer is exact, signalled (a program can catch it) or silent
// (wrong without a signal); big number = share of silent answers (lower is better, green <= 10 %, red > 50 %)
const awareN = (a) => a.exact + a.signalled + a.silent_wrong;
const awareShare = (a) => (a && awareN(a) ? a.silent_wrong / awareN(a) * 100 : null);
const awareColor = (share) => (share <= THRESH.silentGreen ? "g-green" : share <= THRESH.silentYellow ? "g-yellow" : "g-red");
const awareText = (a) => { const sh = awareShare(a); return sh == null ? "-" : `${fmt(sh, 0, " %")} (${a.silent_wrong} of ${awareN(a)})`; };
function awareNote(a) {
  if (!a) return "";
  const n = awareN(a), out = [];
  if (n) out.push(`exact ${a.exact} · signalled ${a.signalled} · silent ${a.silent_wrong} (of ${n} answers)`);
  if (a.total) out.push(`question cut off (overflow test): ${a.caught} of ${a.total} signalled` +
    (a.free_text ? `, ${a.free_text} more only in prose - a program cannot catch that` : ""));
  if (a.decoy && a.decoy.n) out.push(`escape word inside the data: ${a.decoy.right} of ${a.decoy.n} right, ${a.decoy.false_alarm} false alarm${a.decoy.false_alarm === 1 ? "" : "s"} (unsolvable although solvable)`);
  return out.join(" · ");
}
// faithfulness gauge (vision, read-versus-recall prompts): where did the answers come from? read / signalled are faithful
const FAITH_PARTS = [["read", "gs-green", "read: right and legible in the image"], ["signalled", "gs-sig", "signalled: said unsolvable"],
  ["known", "gs-known", "known: right for the wrong reason - world knowledge"], ["guessed", "gs-red", "guessed: an unreadable part answered anyway"],
  ["misread", "gs-mis", "misread: a legible part answered wrong"]];
const faithShare = (f) => (f && f.n ? (f.read + f.signalled) / f.n * 100 : null);
function faithNote(f, smoke) {
  if (!f || !f.n) return "";
  return `read ${f.read} · signalled ${f.signalled} · known ${f.known} · guessed ${f.guessed} · misread ${f.misread} (of ${f.n} items)` +
    (smoke != null ? ` · did not leak the real answer: ${fmt(smoke, 0, " %")}` : "");
}

// key figures of a single run: only the performance numbers here; the quality gauges come from qualityKpis()
function runKpis(d) {
  const ok = d.single.filter((p) => !isAdaptive(p.prompt_id));  // sized by the context: not comparable across runs
  const k = [["Decode tok/s", avg(ok.map((p) => p.decode_tps)), 1, ""], ["TTFA", avg(ok.map((p) => p.ttfa)), 2, " s"],
    ["TTFT", avg(ok.map((p) => p.ttft)), 2, " s"],
    ["Max throughput", d.load.length ? Math.max(...d.load.map((l) => l.throughput ?? 0)) : null, 1, " tok/s"],
    ["Output tok/response", avg(ok.map((p) => p.completion_tokens)), 0, ""]];
  if (d.tokens.per_correct) k.push(["Tokens/correct answer", d.tokens.per_correct, 0, ""]);
  if (d.tokens.total.cost != null) k.push(["Cost", d.tokens.total.cost, 4, currencyUnit(d.tokens.price)]);
  const LABEL = { TTFA: "TTFA (time to first answer)", TTFT: "TTFT (time to first token)" };  // the tooltips stay keyed by name
  return k.map(([name, v, digits, unit]) => ({ name, label: LABEL[name], values: [v], digits, unit }));
}

