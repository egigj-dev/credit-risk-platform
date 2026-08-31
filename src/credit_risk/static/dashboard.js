const $ = (selector) => document.querySelector(selector);
let selectedId = null;
let portfolioClients = [];

const TIER_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
const TIER_COLORS = {LOW:"#168b75", MEDIUM:"#d69a2d", HIGH:"#e06c44", CRITICAL:"#c63f58"};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;"
  })[char]);
}

function percent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "—";
}

function money(value) {
  return new Intl.NumberFormat("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0})
    .format(Number(value) || 0);
}

function tierClass(value) {
  return TIER_ORDER.includes(value) ? value : "MEDIUM";
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = "Request failed";
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

async function loadMode() {
  const ready = await api("/health/ready");
  const label = ready.mode === "production" ? "Production portfolio" :
    ready.mode === "database" ? "Database portfolio" : "Demo portfolio";
  $("#run-state").textContent = `${label} · ready`;
}

function renderMetrics(summary) {
  const protectedCount = Number(summary.protected_client_count ?? summary.high_risk_count ?? 0);
  const cards = [
    ["Clients", Number(summary.client_count).toLocaleString(), "Active portfolio", "neutral"],
    ["Average PD", percent(summary.mean_pd), "Portfolio default probability", "blue"],
    ["Support first", protectedCount.toLocaleString(), "Marketing paused by policy", "red"],
    ["NPL watchlist", Number(summary.npl_watchlist_count).toLocaleString(), "Early-warning priority", "amber"],
    ["Eligible offers", Number(summary.offer_count).toLocaleString(), "After risk and cooldown gates", "green"],
  ];
  $("#metrics").innerHTML = cards.map(([title, value, note, tone]) =>
    `<article class="metric ${tone}"><div class="metric-top"><p>${title}</p><span class="metric-icon"></span></div><strong>${value}</strong><span>${note}</span></article>`
  ).join("");
}

function renderPortfolioVisuals() {
  const counts = Object.fromEntries(TIER_ORDER.map((tier) => [tier, 0]));
  portfolioClients.forEach((client) => {
    const tier = tierClass(client.pd_tier);
    counts[tier] += 1;
  });
  const total = Math.max(portfolioClients.length, 1);
  let cursor = 0;
  const stops = TIER_ORDER.map((tier) => {
    const start = cursor;
    cursor += counts[tier] / total * 360;
    return `${TIER_COLORS[tier]} ${start}deg ${cursor}deg`;
  }).join(",");

  const buckets = [
    {label:"< 12%", lo:0, hi:.12, tier:"LOW"},
    {label:"12–28%", lo:.12, hi:.28, tier:"MEDIUM"},
    {label:"28–50%", lo:.28, hi:.5, tier:"HIGH"},
    {label:"≥ 50%", lo:.5, hi:Infinity, tier:"CRITICAL"},
  ].map((bucket) => ({
    ...bucket,
    count: portfolioClients.filter((c) => Number(c.pd_score) >= bucket.lo && Number(c.pd_score) < bucket.hi).length
  }));
  const maxBucket = Math.max(...buckets.map((b) => b.count), 1);

  $("#portfolio-visuals").innerHTML = `
    <article class="chart-card risk-mix">
      <div><p class="eyebrow">RISK MIX</p><h2>Portfolio distribution</h2><p class="chart-note">PD tier across the active client queue</p></div>
      <div class="donut" style="--segments:conic-gradient(${stops})" role="img" aria-label="Risk tier distribution">
        <div><strong>${total.toLocaleString()}</strong><span>clients</span></div>
      </div>
      <div class="legend">${TIER_ORDER.map((tier) =>
        `<span><i style="background:${TIER_COLORS[tier]}"></i><b>${tier}</b> ${counts[tier]}</span>`
      ).join("")}</div>
    </article>
    <article class="chart-card distribution">
      <div><p class="eyebrow">PD DISTRIBUTION</p><h2>Where risk is concentrated</h2><p class="chart-note">Policy thresholds shown as score bands</p></div>
      <div class="histogram">${buckets.map((bucket) =>
        `<div class="hist-column"><span class="hist-value">${bucket.count}</span><div class="hist-bar ${bucket.tier}" style="height:${Math.max(8, bucket.count/maxBucket*100)}%"></div><span>${bucket.label}</span></div>`
      ).join("")}</div>
    </article>`;
}

async function loadSummary() {
  const [summary, clients] = await Promise.all([
    api("/api/v1/summary"),
    api("/api/v1/clients?query=&risk="),
  ]);
  portfolioClients = clients;
  renderMetrics(summary);
  renderPortfolioVisuals();
}

async function loadClients() {
  const query = encodeURIComponent($("#search").value);
  const risk = encodeURIComponent($("#risk-filter").value);
  const rows = await api(`/api/v1/clients?query=${query}&risk=${risk}`);
  $("#client-count").textContent = `${rows.length} shown`;
  $("#clients").innerHTML = rows.map((client) => {
    const score = Math.min(100, Math.max(0, Number(client.pd_score) * 100));
    return `<button class="client ${client.client_id === selectedId ? "selected" : ""}" data-id="${client.client_id}">
      <span class="client-main"><span class="avatar">${escapeHtml(client.name).slice(0, 1)}</span><span><strong>${escapeHtml(client.name)}</strong><small>${escapeHtml(client.segment)} · ${percent(client.pd_score)} PD</small><i class="risk-line"><i style="width:${score}%"></i></i></span></span>
      <span class="tier ${tierClass(client.pd_tier)}">${escapeHtml(client.pd_tier)}</span>
    </button>`;
  }).join("") || "<div class='list-empty'>No clients match these filters.</div>";
  document.querySelectorAll(".client").forEach((element) =>
    element.addEventListener("click", () => showClient(Number(element.dataset.id)))
  );
}

function gauge(title, score, tier) {
  const degrees = Math.min(100, Math.max(0, Number(score) * 100)) * 3.6;
  return `<article class="score-card">
    <div class="score-gauge ${tierClass(tier)}" style="--gauge:${degrees}deg"><div><strong>${percent(score)}</strong><span>${escapeHtml(tier)}</span></div></div>
    <div><p>${title}</p><span>Model score</span></div>
  </article>`;
}

function featureValue(value) {
  if (value === null || value === undefined) return "combined";
  const n = Number(value);
  if (!Number.isFinite(n)) return escapeHtml(value);
  return Math.abs(n) <= 1 ? n.toFixed(3) : n.toLocaleString(undefined, {maximumFractionDigits:2});
}

function shapChart(title, rows) {
  if (!rows?.length) return `<div class="shap-panel"><h4>${title}</h4><p class="chart-note">No explanation rows are available for this score.</p></div>`;
  const max = Math.max(...rows.map((row) => Math.abs(Number(row.shap_value) || 0)), .0001);
  return `<div class="shap-panel"><div class="shap-heading"><h4>${title}</h4><span>← lowers risk · raises risk →</span></div>
    <div class="shap-axis"></div>
    ${rows.map((row) => {
      const value = Number(row.shap_value) || 0;
      const magnitude = Math.max(2, Math.abs(value) / max * 47);
      const direction = value >= 0 ? "positive" : "negative";
      return `<div class="shap-row" title="${escapeHtml(row.feature)} contribution ${value >= 0 ? "+" : ""}${value.toFixed(4)}">
        <div class="shap-label"><strong>${escapeHtml(row.feature)}</strong><span>value: ${featureValue(row.value)}</span></div>
        <div class="shap-track"><i class="${direction}" style="--magnitude:${magnitude}%"></i></div>
        <b class="${direction}">${value >= 0 ? "+" : ""}${value.toFixed(3)}</b>
      </div>`;
    }).join("")}
  </div>`;
}

function effectivePolicy(client) {
  if (client.recommendation_policy) return client.recommendation_policy;
  const critical = client.pd_tier === "CRITICAL" || client.npl_tier === "CRITICAL";
  const high = client.pd_tier === "HIGH" || client.npl_tier === "HIGH" || client.npl_tier === "CONCERN";
  if (critical || high) return {
    mode: critical ? "INTERVENE" : "SUPPORT",
    title: critical ? "Immediate support and risk intervention" : "Stabilise before recommending products",
    message: "Product marketing is paused until risk and affordability checks are complete.",
    actions: [],
  };
  return {mode:"GROW", title:"Eligible for responsible recommendations", message:"Normal eligibility and suitability controls apply.", actions:[]};
}

function actionPanel(client) {
  const policy = effectivePolicy(client);
  const protectedMode = ["SUPPORT", "INTERVENE"].includes(policy.mode);
  const actions = (policy.actions || []).map((action, index) =>
    `<div class="action"><span>${index + 1}</span><div><small>${escapeHtml(action.priority)}</small><strong>${escapeHtml(action.title)}</strong><p>${escapeHtml(action.detail)}</p></div></div>`
  ).join("");
  const offers = !protectedMode && client.offers?.length ? client.offers.map((offer) =>
    `<div class="offer"><span class="offer-rank">#${offer.rank}</span><div class="offer-copy"><strong>${escapeHtml(offer.product_name)}</strong><p>${escapeHtml(offer.reason)}</p><div class="fit"><i style="width:${Math.max(0, Math.min(100, Number(offer.confidence)*100))}%"></i></div><small>${Math.round(Number(offer.confidence)*100)}% recommendation fit</small></div><button data-offer="${escapeHtml(offer.product_code)}">Mark accepted</button></div>`
  ).join("") : "";
  const empty = !protectedMode && !offers ? "<div class='soft-empty'>No eligible offer remains after ownership and cooldown checks.</div>" : "";
  return `<section class="block action-block">
    <div class="section-heading"><div><p class="eyebrow">DECISION POLICY</p><h3>${protectedMode ? "Next best actions" : "Responsible recommendations"}</h3></div><span class="policy-mode ${policy.mode}">${policy.mode}</span></div>
    <div class="policy-banner ${policy.mode}"><strong>${escapeHtml(policy.title)}</strong><p>${escapeHtml(policy.message)}</p></div>
    <div class="action-list">${actions}</div>${offers}${empty}<p id="notice" class="notice"></p>
  </section>`;
}

function formatUpdated(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString();
}

async function showClient(clientId) {
  selectedId = clientId;
  $("#detail").classList.add("loading");
  try {
    const c = await api(`/api/v1/clients/${clientId}`);
    const explanations = c.model_explanations || {type:"unavailable", pd:[], npl:[]};
    const explanationLabel = explanations.type === "shap" ? "SHAP · production model" : "Additive contribution · demo model";
    $("#detail").innerHTML = `
      <div class="profile-head"><div class="profile-identity"><span class="profile-avatar">${escapeHtml(c.name).slice(0,1)}</span><div><p class="eyebrow">CLIENT ${c.client_id}</p><h2>${escapeHtml(c.name)}</h2><p>${escapeHtml(c.segment)}</p></div></div><div class="updated"><span>Last scored</span><strong>${formatUpdated(c.updated_at)}</strong></div></div>
      <div class="score-grid">${gauge("Probability of default", c.pd_score, c.pd_tier)}${gauge("NPL early warning", c.npl_score, c.npl_tier)}
        <article class="exposure-card"><p>Exposure signals</p><div><strong>${percent(c.utilisation)}</strong><span>utilisation</span></div><div><strong>${Number(c.days_past_due).toLocaleString()}</strong><span>days past due</span></div></article>
      </div>
      <section class="explain-block"><div class="section-heading"><div><p class="eyebrow">MODEL EXPLANATION</p><h3>What moved these scores?</h3></div><span class="explain-type">${explanationLabel}</span></div>
        <p class="explain-note">Positive contributions push predicted risk higher; negative contributions reduce it. They explain model behavior—not causality or a reason to treat the client adversely.</p>
        <div class="shap-grid">${shapChart("PD drivers", explanations.pd)}${shapChart("NPL drivers", explanations.npl)}</div>
      </section>
      <div class="detail-grid">
        <section class="block context-block"><div class="section-heading"><div><p class="eyebrow">CLIENT CONTEXT</p><h3>Financial snapshot</h3></div></div>
          <div class="facts"><div class="fact"><span>Annual income</span><strong>${money(c.annual_income)}</strong></div><div class="fact"><span>Current products</span><strong>${c.owned_products.length}</strong></div></div>
          <h4>Product relationship</h4><div class="product">${c.owned_products.length ? c.owned_products.map((p) => `<span>${escapeHtml(p).replaceAll("_", " ")}</span>`).join("") : "<span>No recorded products</span>"}</div>
        </section>
        ${actionPanel(c)}
      </div>`;
    document.querySelectorAll("[data-offer]").forEach((element) =>
      element.addEventListener("click", () => acceptOffer(clientId, element.dataset.offer))
    );
    await loadClients();
  } finally {
    $("#detail").classList.remove("loading");
  }
}

async function acceptOffer(clientId, productCode) {
  try {
    await api(`/api/v1/clients/${clientId}/offers/accept`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({product_code:productCode})
    });
    await Promise.all([loadSummary(), showClient(clientId)]);
    const notice = $("#notice");
    if (notice) notice.textContent = "Acceptance recorded and removed from the active queue.";
  } catch (error) {
    const notice = $("#notice");
    if (notice) notice.textContent = error.message;
  }
}

$("#search").addEventListener("input", loadClients);
$("#risk-filter").addEventListener("change", loadClients);
$("#run-pipeline").addEventListener("click", async (event) => {
  event.target.disabled = true;
  $("#run-state").textContent = "Running canonical pipeline…";
  try {
    const result = await api("/api/v1/pipeline/run", {method:"POST"});
    $("#run-state").textContent = `${result.run_id || result.run_date || "Pipeline"} completed successfully`;
    await Promise.all([loadSummary(), loadClients()]);
    if (selectedId) await showClient(selectedId);
  } catch (error) {
    $("#run-state").textContent = error.message;
  } finally {
    event.target.disabled = false;
  }
});

Promise.all([loadMode(), loadSummary(), loadClients()]).catch((error) => {
  document.body.innerHTML = `<main class="fatal"><p class="eyebrow">CONNECTION ERROR</p><h1>Could not load the portfolio.</h1><p>${escapeHtml(error.message)}</p></main>`;
});
