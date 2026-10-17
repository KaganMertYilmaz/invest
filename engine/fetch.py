"""Financial Datasets API'den veri çeker, data/cache/ içine ticker bazlı JSON yazar.

Cache dosyası formatı (data/cache/TICKER.json):
{
  "prices": {"fetched_at": "ISO-8601", "price": float, "high_52w": float},
  "fundamentals": {
    "fetched_at": "ISO-8601",
    "income_ttm": [...],        # period=ttm, limit=5 (güncel + 4 çeyrek geri = önceki TTM)
    "income_quarterly": [...],  # period=quarterly, limit=4
    "balance": {...},           # period=quarterly, en güncel
    "cash_flow_ttm": {...}      # period=ttm, en güncel
  }
}
"""
import datetime as dt
import json

import requests

import config


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(path: str, params: dict) -> dict:
    resp = requests.get(
        config.BASE_URL + path,
        headers={"X-API-KEY": config.FD_API_KEY},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def cache_path(ticker: str):
    return config.CACHE_DIR / f"{ticker.replace('.', '-')}.json"


def load_cache(ticker: str) -> dict:
    path = cache_path(ticker)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(ticker: str, cache: dict) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path(ticker), "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def age_days(fetched_at: str | None) -> float:
    """ISO tarih damgasından bugüne geçen gün sayısı. Damga yoksa sonsuz."""
    if not fetched_at:
        return float("inf")
    ts = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 86400


def fetch_prices(ticker: str) -> dict:
    """Tek istekte güncel fiyat + 52 haftalık zirve (1 yıllık haftalık mumlar)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=370)
    data = _get("/prices", {
        "ticker": ticker,
        "interval": "week",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    })
    prices = data.get("prices") or []
    if not prices:
        raise ValueError(f"{ticker}: fiyat verisi boş döndü")
    prices.sort(key=lambda p: p["time"])

    # Split tespiti: önceki kapanış / sonraki açılış >= 1.8 ise split varsay,
    # split öncesi mumları bu oranla böl (API verisi split'e göre ayarlanmamış olabilir).
    factors = [1.0] * len(prices)
    cum = 1.0
    for i in range(len(prices) - 1, 0, -1):
        ratio = prices[i - 1]["close"] / prices[i]["open"] if prices[i]["open"] else 0
        if ratio >= 1.8:
            cum *= ratio
        factors[i - 1] = cum

    return {
        "fetched_at": _now_iso(),
        "price": prices[-1]["close"],
        "high_52w": max(p["high"] / f for p, f in zip(prices, factors)),
        "split_adjusted": cum != 1.0,
    }


def fetch_fundamentals(ticker: str) -> dict:
    """4 istek: income ttm, income quarterly, balance sheet, cash flow ttm."""
    inc_ttm = _get("/financials/income-statements",
                   {"ticker": ticker, "period": "ttm", "limit": 5})
    inc_q = _get("/financials/income-statements",
                 {"ticker": ticker, "period": "quarterly", "limit": 6})
    bal = _get("/financials/balance-sheets",
               {"ticker": ticker, "period": "quarterly", "limit": 1})
    cf = _get("/financials/cash-flow-statements",
              {"ticker": ticker, "period": "ttm", "limit": 1})

    income_ttm = inc_ttm.get("income_statements") or []
    income_q = inc_q.get("income_statements") or []
    balances = bal.get("balance_sheets") or []
    cash_flows = cf.get("cash_flow_statements") or []
    if not income_ttm or not balances:
        raise ValueError(f"{ticker}: temel veri boş döndü")

    return {
        "fetched_at": _now_iso(),
        "income_ttm": income_ttm,
        "income_quarterly": income_q,
        "balance": balances[0],
        "cash_flow_ttm": cash_flows[0] if cash_flows else None,
    }


def refresh_ticker(ticker: str, want_price: bool, fundamentals_max_age_days: int) -> tuple[dict, int, str | None]:
    """Gerekiyorsa veri çeker, cache'i günceller.

    Dönüş: (cache, kullanılan istek sayısı, hata mesajı veya None)
    Hata olursa eldeki cache ile devam edilir; ticker atlanır, skorlama cache'ten yapılır.
    """
    cache = load_cache(ticker)
    credits = 0
    error = None

    if want_price:
        try:
            cache["prices"] = fetch_prices(ticker)
            credits += 1
        except Exception as e:  # noqa: BLE001 - tek ticker hatası koşuyu durdurmasın
            error = f"price: {e}"

    fund = cache.get("fundamentals") or {}
    if age_days(fund.get("fetched_at")) > fundamentals_max_age_days:
        try:
            cache["fundamentals"] = fetch_fundamentals(ticker)
            credits += config.FUNDAMENTALS_REQUESTS
        except Exception as e:  # noqa: BLE001
            error = (error + "; " if error else "") + f"fundamentals: {e}"

    if credits:
        save_cache(ticker, cache)
    return cache, credits, error
