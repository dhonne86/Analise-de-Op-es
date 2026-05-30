from __future__ import annotations

import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
API_BASE = os.getenv("OPLAB_API_BASE", "https://api.oplab.com.br").rstrip("/")
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.105"))


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
    return spot, options, ["Modo demo ativo: configure credenciais da OpLab para dados reais."]


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
    score += max(-20, min(20, edge / max(mid, 0.1) * 35))
    score -= min(20, spread_pct * 80)
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
        spot, raw_options, warnings = fetch_oplab(symbol)
        if not spot or not raw_options:
            raise RuntimeError("Dados insuficientes retornados pela OpLab.")
        source = "oplab"
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
    ivs = [item["iv"] for item in options if item["iv"] > 0]
    spreads = [item["spreadPct"] for item in options if item["mid"] > 0]
    top = sorted(options, key=lambda item: item["score"], reverse=True)[:8]
    hedges = sorted(puts, key=lambda item: (abs(item["delta"] + 0.35), item["spreadPct"]))[:5]
    income = sorted(calls, key=lambda item: (abs(item["delta"] - 0.25), -item["score"]))[:5]
    alerts = []
    if spreads and statistics.median(spreads) > 0.18:
        alerts.append("Spreads medianos elevados: prefira ordens limitadas e reduza tamanho.")
    if ivs and statistics.median(ivs) > 0.45:
        alerts.append("Volatilidade implicita acima do usual: premio rico, bom para estruturas vendidas com risco definido.")
    if ivs and statistics.median(ivs) < 0.22:
        alerts.append("Volatilidade implicita baixa: compras direcionais e travas debitadas ficam relativamente mais atraentes.")
    if not alerts:
        alerts.append("Cadeia equilibrada: priorize liquidez, vencimentos de 20 a 60 dias e risco definido.")
    return {
        "symbol": symbol.upper(),
        "source": source,
        "spot": spot,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "contracts": len(options),
            "calls": len(calls),
            "puts": len(puts),
            "medianIv": statistics.median(ivs) if ivs else 0,
            "medianSpread": statistics.median(spreads) if spreads else 0,
            "bestScore": top[0]["score"] if top else 0,
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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Analista de Opcoes em http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
