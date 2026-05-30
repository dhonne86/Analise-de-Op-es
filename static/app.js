const state = {
  data: null,
  tab: "hedges",
};

const fmtMoney = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const fmtPct = new Intl.NumberFormat("pt-BR", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmtNum = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

const el = (id) => document.getElementById(id);

function setStatus(message) {
  el("status").textContent = message;
}

function metric(label, value) {
  return `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`;
}

function optionItem(item) {
  const edge = item.edge >= 0 ? "+" : "";
  return `
    <div class="optionItem">
      <div>
        <strong>${item.symbol}</strong>
        <small>${item.type.toUpperCase()} ${fmtMoney.format(item.strike)} / ${item.dte} dias / VI ${fmtPct.format(item.iv)}</small>
        <small>Mid ${fmtMoney.format(item.mid)} | Valor justo ${fmtMoney.format(item.fair)} | Edge ${edge}${fmtMoney.format(item.edge)}</small>
      </div>
      <div class="score">${Math.round(item.score)}</div>
    </div>
  `;
}

function renderMetrics(summary) {
  el("metrics").innerHTML = [
    metric("Contratos", summary.contracts),
    metric("Calls", summary.calls),
    metric("Puts", summary.puts),
    metric("VI mediana", fmtPct.format(summary.medianIv)),
    metric("Spread mediano", fmtPct.format(summary.medianSpread)),
  ].join("");
}

function renderIdeas() {
  if (!state.data) return;
  const list = state.tab === "hedges" ? state.data.hedges : state.data.income;
  el("ideaList").innerHTML = list.map(optionItem).join("");
}

function filteredOptions() {
  if (!state.data) return [];
  const type = el("typeFilter").value;
  const minScore = Number(el("scoreFilter").value);
  return state.data.options.filter((item) => {
    const typeOk = type === "all" || item.type === type;
    return typeOk && item.score >= minScore;
  });
}

function renderChain() {
  const options = filteredOptions();
  el("chainCount").textContent = `${options.length} contratos`;
  el("chainBody").innerHTML = options
    .map(
      (item) => `
      <tr>
        <td><strong>${item.symbol}</strong></td>
        <td><span class="pill ${item.type}">${item.type.toUpperCase()}</span></td>
        <td>${fmtMoney.format(item.strike)}</td>
        <td>${item.expiration}</td>
        <td>${item.dte}</td>
        <td>${fmtMoney.format(item.mid)}</td>
        <td>${fmtPct.format(item.iv)}</td>
        <td>${fmtNum.format(item.delta)}</td>
        <td>${fmtPct.format(item.spreadPct)}</td>
        <td><strong>${Math.round(item.score)}</strong></td>
      </tr>
    `
    )
    .join("");
}

function render(data) {
  state.data = data;
  el("headline").textContent = `${data.symbol} em ${data.generatedAt.replace("T", " ")}`;
  const sources = {
    "oplab-free": "OpLab gratis diario",
    "oplab-api": "OpLab API PRO",
    demo: "Fonte demo",
  };
  el("source").textContent = sources[data.source] || "Fonte OpLab";
  el("spot").textContent = fmtMoney.format(data.spot);
  renderMetrics(data.summary);
  el("alerts").innerHTML = data.alerts.map((text) => `<div class="alert">${text}</div>`).join("");
  el("topList").innerHTML = data.top.map(optionItem).join("");
  renderIdeas();
  renderChain();

  const extra = data.warnings && data.warnings.length ? ` | ${data.warnings[0]}` : "";
  setStatus(`${data.options.length} opcoes analisadas${extra}`);
}

async function load(symbol) {
  setStatus(`Buscando snapshot gratuito de ${symbol.toUpperCase()}...`);
  const response = await fetch(`/api/analyze?symbol=${encodeURIComponent(symbol)}`);
  if (!response.ok) throw new Error("Falha ao consultar analise");
  render(await response.json());
}

document.getElementById("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  load(el("symbol").value.trim() || "PETR4").catch((error) => setStatus(error.message));
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.tab = button.dataset.tab;
    renderIdeas();
  });
});

el("typeFilter").addEventListener("change", renderChain);
el("scoreFilter").addEventListener("input", renderChain);

load("PETR4").catch((error) => setStatus(error.message));
