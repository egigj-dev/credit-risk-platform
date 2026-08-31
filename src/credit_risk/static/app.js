const $ = (selector) => document.querySelector(selector);
let selectedId = null;

function percent(value) { return `${(value * 100).toFixed(1)}%`; }
function tierClass(value) { return value || "MEDIUM"; }

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error((await response.json()).detail || "Request failed");
  return response.json();
}

async function loadSummary() {
  const summary = await api("/api/v1/summary");
  const cards = [
    ["Clients", summary.client_count.toLocaleString(), "Active portfolio"],
    ["Average PD", percent(summary.mean_pd), "Probability of default"],
    ["High risk", summary.high_risk_count.toLocaleString(), "High + critical PD"],
    ["NPL watchlist", summary.npl_watchlist_count.toLocaleString(), "Early-warning priority"],
    ["Available offers", summary.offer_count.toLocaleString(), "Eligible recommendations"],
  ];
  $("#metrics").innerHTML = cards.map(([title, value, note]) => `<article class="metric"><p>${title}</p><strong>${value}</strong><span>${note}</span></article>`).join("");
}

async function loadMode() {
  const ready = await api("/health/ready");
  const label = ready.mode === "production" ? "Production portfolio" : "Demo portfolio";
  $("#run-state").textContent = `${label} · ready`;
}

async function loadClients() {
  const query = encodeURIComponent($("#search").value);
  const risk = encodeURIComponent($("#risk-filter").value);
  const rows = await api(`/api/v1/clients?query=${query}&risk=${risk}`);
  $("#client-count").textContent = `${rows.length} shown`;
  $("#clients").innerHTML = rows.map((client) => `<button class="client ${client.client_id === selectedId ? "selected" : ""}" data-id="${client.client_id}"><span><strong>${client.name}</strong><br>${client.segment} · ${percent(client.pd_score)} PD</span><span class="tier ${tierClass(client.pd_tier)}">${client.pd_tier}</span></button>`).join("") || "<p class='empty'>No clients match these filters.</p>";
  document.querySelectorAll(".client").forEach((element) => element.addEventListener("click", () => showClient(Number(element.dataset.id))));
}

async function showClient(clientId) {
  selectedId = clientId;
  const c = await api(`/api/v1/clients/${clientId}`);
  const offers = c.offers.length ? c.offers.map((o) => `<div class="offer"><div><strong>${o.product_name}</strong><p>${o.reason} · ${Math.round(o.confidence * 100)}% fit</p></div><button data-offer="${o.product_code}">Accept</button></div>`).join("") : "<p class='empty'>No offer: this client is either protected by the risk gate or has no eligible product.</p>";
  $("#detail").innerHTML = `<div class="profile-head"><div><p class="eyebrow">CLIENT ${c.client_id}</p><h2>${c.name}</h2><p>${c.segment}</p></div><div class="updated">Last pipeline update<br>${new Date(c.updated_at).toLocaleString()}</div></div><div class="score-grid"><article class="score"><p>Probability of default</p><strong>${percent(c.pd_score)}</strong><span class="tier ${c.pd_tier}">${c.pd_tier}</span></article><article class="score"><p>NPL early warning</p><strong>${percent(c.npl_score)}</strong><span class="tier ${c.npl_tier}">${c.npl_tier}</span></article><article class="score"><p>Utilisation</p><strong>${percent(c.utilisation)}</strong><span>${c.days_past_due} days past due</span></article></div><div class="detail-grid"><section class="block"><h3>Risk context</h3><div class="facts"><div class="fact"><span>Annual income</span><strong>$${c.annual_income.toLocaleString()}</strong></div><div class="fact"><span>Days past due</span><strong>${c.days_past_due}</strong></div></div><h3 style="margin-top:20px">Current products</h3><div class="product">${c.owned_products.length ? c.owned_products.map((p) => `<span>${p.replaceAll("_", " ")}</span>`).join("") : "<span>No recorded products</span>"}</div></section><section class="block"><h3>Next best actions</h3><div id="offers">${offers}</div><p id="notice" class="notice"></p></section></div>`;
  document.querySelectorAll("[data-offer]").forEach((element) => element.addEventListener("click", () => acceptOffer(clientId, element.dataset.offer)));
  loadClients();
}

async function acceptOffer(clientId, productCode) {
  try {
    await api(`/api/v1/clients/${clientId}/offers/accept`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({product_code:productCode})});
    $("#notice").textContent = "Offer accepted and removed from the active recommendation queue.";
    await loadSummary(); await showClient(clientId);
  } catch (error) { $("#notice").textContent = error.message; }
}

$("#search").addEventListener("input", () => loadClients());
$("#risk-filter").addEventListener("change", () => loadClients());
$("#run-pipeline").addEventListener("click", async (event) => { event.target.disabled = true; $("#run-state").textContent = "Running canonical pipeline…"; try { const result = await api("/api/v1/pipeline/run", {method:"POST"}); $("#run-state").textContent = `${result.run_id} completed successfully`; await loadSummary(); if (selectedId) await showClient(selectedId); } catch (error) { $("#run-state").textContent = error.message; } finally { event.target.disabled = false; } });

Promise.all([loadMode(), loadSummary(), loadClients()]).catch((error) => { document.body.innerHTML = `<main class="shell"><h1>Could not load portfolio</h1><p>${error.message}</p></main>`; });
