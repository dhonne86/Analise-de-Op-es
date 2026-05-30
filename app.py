from __future__ import annotations

import json
import math
import os
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / "data" / "oplab-free"
API_BASE = os.getenv("OPLAB_API_BASE", "https://api.oplab.com.br").rstrip("/")
FREE_OPLAB_BASE = os.getenv("FREE_OPLAB_BASE", "https://opcoes.oplab.com.br").rstrip("/")
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.105"))
MARKET_TZ = ZoneInfo(os.getenv("MARKET_TZ", "America/Sao_Paulo"))
MARKET_OPEN = os.getenv("MARKET_OPEN", "10:00")
MARKET_CLOSE = os.getenv("MARKET_CLOSE", "18:00")
FREE_CACHE_OPEN_SECONDS = int(os.getenv("FREE_CACHE_OPEN_SECONDS", "900"))
FREE_CACHE_CLOSED_SECONDS = int(os.getenv("FREE_CACHE_CLOSED_SECONDS", "86400"))


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def market_clock(now: datetime | None = None) -> dict[str, Any]:
    now = now.astimezone(MARKET_TZ) if now else datetime.now(MARKET_TZ)
    open_hour, open_minute = parse_hhmm(MARKET_OPEN)
    close_hour, close_minute = parse_hhmm(MARKET_CLOSE)
    open_at = now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    close_at = now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    weekday = now.weekday() < 5
    open_now = weekday and open_at <= now <= close_at
    if not weekday:
        label = "Fechado: fim de semana"
    elif now < open_at:
        label = "Pre-pregao"
    elif open_now:
        label = "Pregao aberto"
    else:
        label = "Fechado"
    return {
        "now": now,
        "isWeekday": weekday,
        "isOpen": open_now,
        "label": label,
        "openAt": open_at,
        "closeAt": close_at,
        "cacheTtl": FREE_CACHE_OPEN_SECONDS if open_now else FREE_CACHE_CLOSED_SECONDS,
        "nextRefreshSeconds": FREE_CACHE_OPEN_SECONDS if open_now else 3600,
    }


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes(spot: float, strike: float, days: int, rate: float, iv: float, kind: str) -> dict[str, float]:
    t = max(days, 1) / 252.0
    vol = max(iv, 0.0001)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if kind == "put":
        price = strike * math.exp(-rate * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)
        delta = norm_cdf(d1) - 1.0
        theta = (-(spot * norm_pdf(d1) * vol) / (2 * math.sqrt(t)) + rate * strike * math.exp(-rate * t) * norm_cdf(-d2)) / 252.0
    else:
        price = spot * norm_cdf(d1) - strike * math.exp(-rate * t) * norm_cdf(d2)
        delta = norm_cdf(d1)
        theta = (-(spot * norm_pdf(d1) * vol) / (2 * math.sqrt(t)) - rate * strike * math.exp(-rate * t) * norm_cdf(d2)) / 252.0
    gamma = norm_pdf(d1) / (spot * vol * math.sqrt(t))
    vega = spot * norm_pdf(d1) * math.sqrt(t) / 100.0
    return {"fair": price, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.replace("%", "").replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value).date()
    raw = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def pick(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


def unwrap_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "options", "symbols", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "calls" in payload or "puts" in payload:
            return [*(payload.get("calls") or []), *(payload.get("puts") or [])]
    return []


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_next_data(html: str) -> Any:
    marker = "__NEXT_DATA__"
    marker_at = html.find(marker)
    if marker_at < 0:
        raise RuntimeError("A pagina publica da OpLab nao trouxe __NEXT_DATA__.")
    script_start = html.rfind("<script", 0, marker_at)
    json_start = html.find(">", script_start) + 1
    json_end = html.find("</script>", json_start)
    if script_start < 0 or json_start <= 0 or json_end < 0:
        raise RuntimeError("Nao foi possivel localizar o JSON publico da OpLab.")
    return json.loads(html[json_start:json_end])


def infer_spot(symbol: str, payload: Any, options: list[dict[str, Any]]) -> float:
    symbol = symbol.upper()
    candidates: list[float] = []
    for item in walk_json(payload):
        item_symbol = str(pick(item, "symbol", "ticker", "code", default="")).upper()
        category = str(pick(item, "category", "type", default="")).upper()
        if item_symbol == symbol and category not in ("CALL", "PUT"):
            for field in ("close", "price", "last", "last_price", "value"):
                value = as_float(item.get(field))
                if value > 0:
                    candidates.append(value)
    if candidates:
        return candidates[0]

    atm_strikes = [
        as_float(item.get("strike"))
        for item in options
        if str(pick(item, "bs", default={}).get("moneyness", "")).upper() == "ATM" and as_float(item.get("strike")) > 0
    ]
    if atm_strikes:
        return statistics.median(atm_strikes)
    strikes = [as_float(item.get("strike")) for item in options if as_float(item.get("strike")) > 0]
    return statistics.median(strikes) if strikes else 0


def infer_source_updated_at(options: list[dict[str, Any]]) -> str | None:
    timestamps: list[datetime] = []
    for item in options:
        raw = item.get("time") or item.get("updated_at") or item.get("datetime")
        if not raw:
            continue
        try:
            timestamps.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(MARKET_TZ))
        except ValueError:
            pass
    if not timestamps:
        return None
    return max(timestamps).isoformat(timespec="seconds")


def normalize_free_leg(leg: dict[str, Any]) -> dict[str, Any]:
    bs = leg.get("bs") if isinstance(leg.get("bs"), dict) else {}
    days = int(as_float(pick(leg, "days_to_maturity", "daysToMaturity", default=pick(bs, "daysToMaturity", default=0))))
    expiration = parse_date(pick(leg, "due_date", "maturity_date", "expiration", "expires_at"))
    if expiration is None and days > 0:
        # OpLab publica dias uteis; esta aproximacao em dias corridos mantem o vencimento util para analise.
        expiration = date.today() + timedelta(days=max(1, round(days * 7 / 5)))
    category = str(pick(leg, "category", "type", default=pick(bs, "type", default=""))).lower()
    return {
        "symbol": pick(leg, "symbol", "ticker", "code", default=""),
        "type": category,
        "strike": pick(leg, "strike", default=pick(bs, "strike", default=0)),
        "due_date": expiration.isoformat() if expiration else None,
        "bid": pick(leg, "bid", default=pick(bs, "bid", default=0)),
        "ask": pick(leg, "ask", default=pick(bs, "ask", default=0)),
        "last": pick(leg, "close", "price", default=pick(bs, "price", "premium", default=0)),
        "iv": pick(bs, "volatility", default=pick(leg, "volatility", "iv", default=0)),
        "volume": pick(leg, "volume", default=0),
        "financial_volume": pick(leg, "financial_volume", default=0),
        "liquidity": pick(leg, "liquidity", default=pick(bs, "liquidity-level", default=0)),
        "delta": pick(bs, "delta", default=pick(leg, "delta", default=0)),
        "gamma": pick(bs, "gamma", default=pick(leg, "gamma", default=0)),
        "theta": pick(bs, "theta", default=pick(leg, "theta", default=0)),
        "vega": pick(bs, "vega", default=pick(leg, "vega", default=0)),
        "time": pick(leg, "time", "updated_at", "datetime", default=None),
    }


def extract_free_options(payload: Any) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in walk_json(payload):
        for side in ("call", "put"):
            leg = item.get(side)
            if isinstance(leg, dict) and leg.get("symbol"):
                normalized = normalize_free_leg(leg)
                key = str(normalized.get("symbol"))
                if key and key not in seen:
                    seen.add(key)
                    options.append(normalized)
        category = str(pick(item, "category", "type", default="")).upper()
        if category in ("CALL", "PUT") and item.get("symbol"):
            normalized = normalize_free_leg(item)
            key = str(normalized.get("symbol"))
            if key and key not in seen:
                seen.add(key)
                options.append(normalized)
    return options


def fetch_oplab_free_daily(symbol: str) -> tuple[float, list[dict[str, Any]], list[str]]:
    symbol = symbol.upper()
    clock = market_clock()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{symbol}-{date.today().isoformat()}.json"
    warnings: list[str] = []
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_at = parse_date(cached.get("cachedAt"))
        age = time.time() - as_float(cached.get("cachedEpoch"), default=0)
        if cached_at == date.today() and age <= clock["cacheTtl"]:
            warnings.append(f"Cache gratuito da OpLab: {cache_file.name}, idade {round(age / 60)} min.")
            return as_float(cached.get("spot")), cached.get("options") or [], warnings

    url = f"{FREE_OPLAB_BASE}/mercado/acoes/opcoes/{urllib.parse.quote(symbol)}"
    req = urllib.request.Request(url, headers={"User-Agent": "analista-opcoes-free/1.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=25) as response:
        html = response.read().decode("utf-8", "ignore")
    payload = extract_next_data(html)
    options = extract_free_options(payload)
    spot = infer_spot(symbol, payload, options)
    if not spot or not options:
        raise RuntimeError("A pagina publica gratuita da OpLab nao trouxe dados suficientes.")
    cache_payload = {
        "symbol": symbol,
        "spot": spot,
        "options": options,
        "cachedAt": datetime.now().isoformat(timespec="seconds"),
        "cachedEpoch": time.time(),
        "sourceUpdatedAt": infer_source_updated_at(options),
        "url": url,
    }
    cache_file.write_text(json.dumps(cache_payload, ensure_ascii=False), encoding="utf-8")
    warnings.append("Snapshot gratuito da OpLab atualizado; cotacoes publicas possuem atraso.")
    return spot, options, warnings


@dataclass
class OplabClient:
    token: str | None = None
    token_expires_at: float = 0

    def headers(self) -> dict[str, str]:
        token = os.getenv("OPLAB_ACCESS_TOKEN") or self.token
        headers = {"Content-Type": "application/json", "User-Agent": "analista-opcoes-local/1.0"}
        if token:
            headers["access-token"] = token
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def authenticate(self) -> None:
        if os.getenv("OPLAB_ACCESS_TOKEN") or self.token_expires_at > time.time() + 60:
            return
        email = os.getenv("OPLAB_EMAIL")
        password = os.getenv("OPLAB_PASSWORD")
        if not email or not password:
            raise RuntimeError("Credenciais da OpLab nao configuradas.")
        payload = json.dumps({"email": email, "password": password}).encode()
        req = urllib.request.Request(
            f"{API_BASE}/v3/domain/users/authenticate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            user = json.loads(response.read().decode("utf-8"))
        self.token = user.get("access-token") or user.get("access_token") or user.get("token")
        self.token_expires_at = time.time() + 3600
        if not self.token:
            raise RuntimeError("A autenticacao respondeu sem token reconhecivel.")

    def get(self, path: str) -> Any:
        self.authenticate()
        url = f"{API_BASE}{path}"
        req = urllib.request.Request(url, headers=self.headers(), method="GET")
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))


CLIENT = OplabClient()


def get_nested_price(payload: Any, fallback: float) -> float:
    if isinstance(payload, dict):
        for candidate in (payload, payload.get("data") or {}, payload.get("quote") or {}, payload.get("ticker") or {}):
            if isinstance(candidate, dict):
                value = pick(candidate, "close", "last", "price", "spot", "last_price", "regularMarketPrice")
                if value is not None:
                    return as_float(value, fallback)
    return fallback


def fetch_oplab(symbol: str) -> tuple[float, list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    underlying_template = os.getenv("OPLAB_UNDERLYING_PATH", "/v3/market/stocks/{symbol}")
    options_template = os.getenv("OPLAB_OPTIONS_PATH", "/v3/market/options/{symbol}")
    underlying_payload = CLIENT.get(underlying_template.format(symbol=urllib.parse.quote(symbol)))
    spot = get_nested_price(underlying_payload, 0)
    options_payload = CLIENT.get(options_template.format(symbol=urllib.parse.quote(symbol)))
    options = unwrap_items(options_payload)
    if not options:
        warnings.append("A resposta da OpLab chegou sem lista de opcoes reconhecida. Ajuste OPLAB_OPTIONS_PATH ou o parser.")
    return spot, options, warnings


def demo_market(symbol: str) -> tuple[float, list[dict[str, Any]], list[str]]:
    random.seed(symbol.upper())
    spot = {"PETR4": 38.25, "VALE3": 62.4, "BOVA11": 126.2, "ITUB4": 35.7}.get(symbol.upper(), 42.0)
    expirations = [date.today() + timedelta(days=d) for d in (18, 32, 60)]
    options: list[dict[str, Any]] = []
    for exp in expirations:
        for offset in (-0.15, -0.1, -0.05, 0, 0.05, 0.1, 0.15):
            strike = round(spot * (1 + offset), 2)
            for kind in ("call", "put"):
                iv = 0.26 + abs(offset) * 0.7 + random.random() * 0.05
                days = max((exp - date.today()).days, 1)
                bs = black_scholes(spot, strike, days, RISK_FREE_RATE, iv, kind)
                spread = max(0.02, bs["fair"] * (0.025 + abs(offset) * 0.08))
                bid = max(0.01, bs["fair"] - spread / 2)
                ask = bs["fair"] + spread / 2
                month_code = "ABCDEFGHJKLM"[exp.month - 1] if kind == "call" else "MNOPQRSTUVWX"[exp.month - 1]
                options.append(
                    {
                        "symbol": f"{symbol[:4].upper()}{month_code}{int(strike * 100) % 1000:03d}",
                        "type": kind,
                        "strike": strike,
                        "due_date": exp.isoformat(),
                        "bid": bid,
                        "ask": ask,
                        "last": (bid + ask) / 2,
                        "iv": iv * 100,
                        "volume": int(random.uniform(20, 5000) * (1 - min(abs(offset), 0.14))),
                        "liquidity": max(0, min(5, 5 - abs(offset) * 18 + random.uniform(-0.6, 0.6))),
                    }
                )
    return spot, options, ["Modo demo ativo: a consulta gratuita diaria da OpLab falhou."]


def normalize_option(raw: dict[str, Any], spot: float) -> dict[str, Any] | None:
    symbol = str(pick(raw, "symbol", "ticker", "code", "name", default="")).upper()
    strike = as_float(pick(raw, "strike", "strike_price", "exercise_price", "exercisePrice"))
    expiration = parse_date(pick(raw, "due_date", "expiration", "expiration_date", "maturity_date", "expires_at"))
    kind = str(pick(raw, "type", "kind", "option_type", "category", default="")).lower()
    if "put" in kind or kind in ("p", "venda"):
        kind = "put"
    elif "call" in kind or kind in ("c", "compra"):
        kind = "call"
    elif len(symbol) >= 5 and symbol[4] in "MNOPQRSTUVWX":
        kind = "put"
    else:
        kind = "call"
    if not strike or not expiration:
        return None
    bid = as_float(pick(raw, "bid", "best_bid", "buy", "bid_price"))
    ask = as_float(pick(raw, "ask", "best_ask", "sell", "ask_price"))
    last = as_float(pick(raw, "last", "close", "price", "last_price"), (bid + ask) / 2 if bid and ask else 0)
    mid = (bid + ask) / 2 if bid and ask else last
    iv_raw = as_float(pick(raw, "iv", "implied_volatility", "volatility", "vol_imp"))
    iv = iv_raw / 100 if iv_raw > 2 else iv_raw
    if iv <= 0:
        iv = 0.32
    days = max((expiration - date.today()).days, 0)
    bs = black_scholes(spot, strike, days, RISK_FREE_RATE, iv, kind)
    spread_pct = (ask - bid) / mid if mid > 0 and ask >= bid and bid > 0 else 1.0
    intrinsic = max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    extrinsic = max(0.0, mid - intrinsic)
    volume = as_float(pick(raw, "volume", "financial_volume", "trades", "quantity"))
    liquidity = as_float(pick(raw, "liquidity", "liquidity_score", "score_liquidity"), min(5, math.log10(max(volume, 1)) + 1))
    moneyness = strike / spot if spot else 0
    edge = bs["fair"] - mid
    score = 50
    score += min(25, liquidity * 5)
    score += max(-20, min(20, edge / mid * 35)) if mid > 0 else -20
    score -= min(20, spread_pct * 80)
    score -= 25 if mid <= 0 or (bid <= 0 and ask <= 0) else 0
    score -= 25 if bid <= 0 or ask <= 0 else 0
    score -= 8 if liquidity <= 0 else 0
    score -= 10 if days < 7 or days > 120 else 0
    score += 8 if 0.9 <= moneyness <= 1.1 else -4
    return {
        "symbol": symbol,
        "type": kind,
        "strike": strike,
        "expiration": expiration.isoformat(),
        "dte": days,
        "bid": bid,
        "ask": ask,
        "last": last,
        "mid": mid,
        "iv": iv,
        "fair": bs["fair"],
        "edge": edge,
        "delta": as_float(pick(raw, "delta"), bs["delta"]),
        "gamma": as_float(pick(raw, "gamma"), bs["gamma"]),
        "theta": as_float(pick(raw, "theta"), bs["theta"]),
        "vega": as_float(pick(raw, "vega"), bs["vega"]),
        "intrinsic": intrinsic,
        "extrinsic": extrinsic,
        "spreadPct": spread_pct,
        "volume": volume,
        "liquidity": liquidity,
        "moneyness": moneyness,
        "score": max(0, min(100, score)),
    }


def build_analysis(symbol: str) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        if os.getenv("OPLAB_USE_PAID_API") == "1":
            spot, raw_options, warnings = fetch_oplab(symbol)
            source = "oplab-api"
        else:
            spot, raw_options, warnings = fetch_oplab_free_daily(symbol)
            source = "oplab-free"
        if not spot or not raw_options:
            raise RuntimeError("Dados insuficientes retornados pela OpLab.")
    except Exception as exc:
        spot, raw_options, warnings = demo_market(symbol)
        warnings.append(str(exc))
        source = "demo"
    options = [item for item in (normalize_option(raw, spot) for raw in raw_options) if item]
    if not options:
        spot, raw_options, warnings = demo_market(symbol)
        options = [item for item in (normalize_option(raw, spot) for raw in raw_options) if item]
        source = "demo"
    calls = [item for item in options if item["type"] == "call"]
    puts = [item for item in options if item["type"] == "put"]
    liquid = [item for item in options if item["bid"] > 0 and item["ask"] > 0 and item["spreadPct"] <= 0.35 and item["dte"] <= 90]
    weeklies = [item for item in liquid if item["dte"] <= 10]
    front_month = [item for item in liquid if 10 < item["dte"] <= 35]
    atm = [item for item in liquid if 0.97 <= item["moneyness"] <= 1.03]
    otm_puts = [item for item in liquid if item["type"] == "put" and 0.9 <= item["moneyness"] < 0.98]
    otm_calls = [item for item in liquid if item["type"] == "call" and 1.02 < item["moneyness"] <= 1.1]
    ivs = [item["iv"] for item in options if item["iv"] > 0]
    liquid_ivs = [item["iv"] for item in liquid if item["iv"] > 0]
    spreads = [item["spreadPct"] for item in options if item["mid"] > 0]
    top_pool = liquid or options
    top = sorted(top_pool, key=lambda item: item["score"], reverse=True)[:8]
    hedges = sorted([item for item in liquid if item["type"] == "put"], key=lambda item: (abs(item["delta"] + 0.35), item["spreadPct"]))[:5]
    income = sorted([item for item in liquid if item["type"] == "call"], key=lambda item: (abs(item["delta"] - 0.25), -item["score"]))[:5]
    alerts = []
    clock = market_clock()
    if source == "oplab-free":
        alerts.append("Fonte gratuita da OpLab: leitura atrasada, adequada para acompanhamento tatico sem execucao automatica.")
    if clock["isOpen"]:
        alerts.append("Pregao aberto: atualizacao automatica ativada durante a janela de mercado.")
    else:
        alerts.append(f"{clock['label']}: mantendo cache mais longo ate a proxima sessao.")
    if spreads and statistics.median(spreads) > 0.18:
        alerts.append("Spreads medianos elevados: prefira ordens limitadas e reduza tamanho.")
    reference_ivs = liquid_ivs or ivs
    if reference_ivs and statistics.median(reference_ivs) > 0.45:
        alerts.append("Volatilidade implicita acima do usual: premio rico, bom para estruturas vendidas com risco definido.")
    if reference_ivs and statistics.median(reference_ivs) < 0.22:
        alerts.append("Volatilidade implicita baixa: compras direcionais e travas debitadas ficam relativamente mais atraentes.")
    if not alerts:
        alerts.append("Cadeia equilibrada: priorize liquidez, vencimentos de 20 a 60 dias e risco definido.")
    put_iv = statistics.median([item["iv"] for item in otm_puts]) if otm_puts else 0
    call_iv = statistics.median([item["iv"] for item in otm_calls]) if otm_calls else 0
    skew = put_iv - call_iv if put_iv and call_iv else 0
    if skew > 0.05:
        pulse = "Defensivo: puts OTM carregam premio acima das calls."
    elif skew < -0.05:
        pulse = "Apetite por alta: calls OTM carregam premio relativo."
    else:
        pulse = "Neutro: skew entre calls e puts esta equilibrado."
    return {
        "symbol": symbol.upper(),
        "source": source,
        "spot": spot,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "contracts": len(options),
            "calls": len(calls),
            "puts": len(puts),
            "liquidContracts": len(liquid),
            "weeklies": len(weeklies),
            "medianIv": statistics.median(ivs) if ivs else 0,
            "liquidMedianIv": statistics.median(liquid_ivs) if liquid_ivs else 0,
            "medianSpread": statistics.median(spreads) if spreads else 0,
            "bestScore": top[0]["score"] if top else 0,
        },
        "market": {
            "session": clock["label"],
            "isOpen": clock["isOpen"],
            "timezone": str(MARKET_TZ),
            "openAt": clock["openAt"].isoformat(timespec="minutes"),
            "closeAt": clock["closeAt"].isoformat(timespec="minutes"),
            "nextRefreshSeconds": clock["nextRefreshSeconds"],
            "liquidContracts": len(liquid),
            "weeklies": len(weeklies),
            "frontMonth": len(front_month),
            "atmIv": statistics.median([item["iv"] for item in atm]) if atm else 0,
            "putCallSkew": skew,
            "pulse": pulse,
        },
        "top": top,
        "hedges": hedges,
        "income": income,
        "alerts": alerts,
        "warnings": warnings,
        "options": sorted(options, key=lambda item: (item["expiration"], item["type"], item["strike"])),
    }


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        if parsed.path.startswith("/api/"):
            return str(ROOT)
        rel = parsed.path.lstrip("/") or "index.html"
        return str(STATIC_DIR / rel)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/analyze":
            query = urllib.parse.parse_qs(parsed.query)
            symbol = (query.get("symbol") or ["PETR4"])[0].strip().upper()
            payload = build_analysis(symbol)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


def main() -> int:
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Analista de Opcoes em http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
