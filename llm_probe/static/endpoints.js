"use strict";
// tabs and endpoints - part of the page, loaded in the order of the <script> tags in index.html
// --------------------------------------------------------------------- tabs
function renderHome() {
  const eps = (state.cfg || { endpoints: [] }).endpoints, runs = state.runs || [];
  $("home-ep").innerHTML = eps.length
    ? `<span class="green">&#10003;</span> ${eps.length} endpoint${eps.length > 1 ? "s" : ""}: ${eps.map((e) => esc(e.name)).join(", ")}`
    : '<span class="yellow">No endpoint yet - start here.</span>';
  $("home-runs").innerHTML = runs.length
    ? `${runs.length} stored run${runs.length > 1 ? "s" : ""}, the latest: #${runs.reduce((a, b) => (b.id > a.id ? b : a)).id}`
    : '<span class="dim">No runs yet.</span>';
  $("home-start").textContent = eps.length ? "Go to Test" : "Add an endpoint";
  $("home-start").dataset.goto = eps.length ? "test" : "ep";
}
document.querySelectorAll("[data-goto]").forEach((el) => el.addEventListener("click", (ev) => {
  ev.preventDefault(); showTab(el.dataset.goto);
}));
const SETTINGS_TABS = ["ep", "prompts"];  // sub-tabs of Settings
// Every tab remembers its scroll position: switching away and back lands where you were. fresh = true for an explicit
// new selection (Show responses, Compare marked, Reuse for test ...), which starts at the top of the target tab.
state.tabScroll = {};
let currentTab = null;
function showTab(name, fresh = false) {
  if (name === "settings") name = recall("settings-tab") || "ep";
  if (currentTab && currentTab !== name) state.tabScroll[currentTab] = window.scrollY;
  const top = SETTINGS_TABS.includes(name) ? "settings" : name;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === top));
  $("subtabs").hidden = top !== "settings";
  document.querySelectorAll("#subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  storeTab(name);
  if (name === "home") renderHome();
  if (top === "settings") store("settings-tab", name);
  for (const box of [$("log"), $("out")]) if (box.dataset.follow !== "") box.scrollTop = box.scrollHeight;
  if (currentTab !== name) window.scrollTo(0, fresh ? 0 : state.tabScroll[name] || 0);
  else if (fresh) window.scrollTo(0, 0);
  currentTab = name;
  if (state.staleViews && state.staleViews.has(name)) refreshStale();
}
document.querySelectorAll("#tabs button, #subtabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- endpoints
async function loadConfig() {
  state.cfg = await api("/api/config");
  const tb = $("ep-table").querySelector("tbody");
  tb.innerHTML = state.cfg.endpoints.map((e) =>
    `<tr class="click${e.name === state.epOriginal ? " sel" : ""}" data-name="${esc(e.name)}"><td${tipAttr(e.base_url)}>${esc(e.name)}</td>` +
    `<td class="l"${tipAttr(KIND_TIPS[e.kind])}>${esc(e.kind)}</td>` +
    `<td${tipAttr(e.num_ctx > 0 ? "num_ctx set for this endpoint" : "Server default (OLLAMA_CONTEXT_LENGTH / Modelfile / max_model_len)")}>` +
    `${["ollama", "openwebui"].includes(e.kind) && e.num_ctx > 0 ? e.num_ctx : "default"}</td>` +
    `<td class="l"${tipAttr(e.ssh_host ? `${e.ssh_user ? e.ssh_user + "@" : ""}${e.ssh_host}:${e.ssh_port}` : "No SSH host - no resource monitoring")}>` +
    `${e.ssh_host ? "yes" : "-"}</td></tr>`).join("") ||
    '<tr><td colspan="4" class="dim">No endpoints yet - fill in the form and save.</td></tr>';
  tb.querySelectorAll("tr[data-name]").forEach((tr) => tr.addEventListener("click", () => editEndpoint(tr.dataset.name)));
  const sel = $("t-ep");
  const before = sel.value || recall("endpoint") || "";
  fillSelect(sel, state.cfg.endpoints.map((e) => [`${e.name} (${e.kind})`, e.name]), "Select endpoint", false);
  if (state.cfg.endpoints.some((e) => e.name === before)) sel.value = before;
  if (sel.value !== state.modelsFor) loadModels();
}

function editEndpoint(name) {
  const e = state.cfg.endpoints.find((x) => x.name === name) ||
    { name: "", kind: "ollama", base_url: "", ssh_host: "", ssh_port: 22, ssh_user: "", ssh_key_file: "" };
  state.epOriginal = name || null;
  $("ep-name").value = e.name; $("ep-kind").value = e.kind; $("ep-url").value = e.base_url;
  $("ep-ctx").value = e.num_ctx > 0 ? e.num_ctx : "";
  $("ep-price-in").value = e.price_in || ""; $("ep-price-out").value = e.price_out || ""; $("ep-currency").value = e.currency || ""; showPriceUnit();
  $("ep-key").value = ""; $("ep-key").placeholder = e.has_api_key ? "(stored - leave empty to keep)" : "";
  $("ep-ssh-host").value = e.ssh_host; $("ep-ssh-port").value = e.ssh_port; $("ep-ssh-user").value = e.ssh_user;
  $("ep-ssh-pw").value = ""; $("ep-ssh-pw").placeholder = e.has_ssh_password ? "(stored - leave empty to keep)" : "";
  $("ep-ssh-keyfile").value = e.ssh_key_file; $("ep-clear").checked = false;
  $("ep-del").disabled = !state.epOriginal;
  setMsg($("ep-status"), "");
  $("ep-table").querySelectorAll("tr[data-name]").forEach((tr) => tr.classList.toggle("sel", tr.dataset.name === name));
}

function endpointForm() {
  return {
    original: state.epOriginal, name: $("ep-name").value, kind: $("ep-kind").value, base_url: $("ep-url").value,
    api_key: $("ep-key").value, num_ctx: $("ep-ctx").value, price_in: $("ep-price-in").value, currency: $("ep-currency").value,
    price_out: $("ep-price-out").value, ssh_host: $("ep-ssh-host").value, ssh_port: $("ep-ssh-port").value,
    ssh_user: $("ep-ssh-user").value, ssh_password: $("ep-ssh-pw").value, ssh_key_file: $("ep-ssh-keyfile").value,
    clear_secrets: $("ep-clear").checked,
  };
}

$("ep-kind").addEventListener("change", () => {
  // swap the default port of the other kind automatically
  const ports = state.cfg.default_ports, port = ports[$("ep-kind").value], url = $("ep-url");
  if (!port) return;
  for (const other of Object.values(ports)) {
    if (other !== port && url.value.endsWith(":" + other)) url.value = url.value.slice(0, -String(other).length) + port;
  }
});
$("ep-new").addEventListener("click", () => { editEndpoint(""); $("ep-name").focus(); });
$("ep-save").addEventListener("click", async () => {
  try {
    const r = await api("/api/endpoints", { method: "POST", body: endpointForm() });
    state.epOriginal = r.endpoint.name;
    await loadConfig();
    editEndpoint(r.endpoint.name);
    setMsg($("ep-status"), "Saved.", "ok");
  } catch (e) { setMsg($("ep-status"), e.message, "err"); }
});
$("ep-del").addEventListener("click", async () => {
  if (!state.epOriginal || !confirm(`Delete endpoint '${state.epOriginal}'?`)) return;
  try {
    await api("/api/endpoints/" + encodeURIComponent(state.epOriginal), { method: "DELETE" });
    state.epOriginal = null;
    await loadConfig();
    editEndpoint(state.cfg.endpoints.length ? state.cfg.endpoints[0].name : "");
  } catch (e) { setMsg($("ep-status"), e.message, "err"); }
});
for (const [id, path, label] of [["ep-test-api", "test-api", "Querying API"], ["ep-test-ssh", "test-ssh", "Connecting via SSH"]]) {
  $(id).addEventListener("click", async () => {
    setMsg($("ep-status"), label + " ...", "dim");
    try {
      const r = await api("/api/endpoints/" + path, { method: "POST", body: endpointForm() });
      setMsg($("ep-status"), r.message, r.ok ? "ok" : "err");
    } catch (e) { setMsg($("ep-status"), e.message, "err"); }
  });
}

