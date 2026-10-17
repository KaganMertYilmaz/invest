"""Financial Datasets API'den veri çeker, data/cache/ içine ticker bazlı JSON yazar.

Cache dosyası formatı (data/cache/TICKER.json):
{
  "prices": {
    "fetched_at": "ISO-8601",
    "price": float,
    "price_source": "snapshot" | "close",
    "price_date": "YYYY-MM-DD",       # snapshot zamanı veya son tamamlanmış barın günü
    "high_52w": float,                # haftalık mumlardan, split'e göre düzeltilmiş
    "high_52w_fetched_at": "ISO-8601",
    "split_adjusted": bool
  },
  "fundamentals": {
    "fetched_at": "ISO-8601",
    "income_ttm": [...],        # period=ttm, limit=5 (güncel + 4 çeyrek geri = önceki TTM)
    "income_quarterly": [...],  # period=quarterly, limit=6
    "balance": {...},           # period=quarterly, en güncel
    "cash_flow_ttm": {...}      # period=ttm, en güncel
  }
}

Endeks cache dosyası (data/cache/SPY.json):
{
  "daily": {"fetched_at": "ISO-8601", "bars": [{"time": "YYYY-MM-DD", "close": float}, ...]}
}
"""
import datetime as dt
import json

import requests

import config

# Çalışma boyunca yapılan toplam HTTP isteği (maliyet takibi için)
REQUEST_COUNT = 0

# 52 haftalık zirve kaç günde bir yenilensin (her çalışmada sadece snapshot çekilir)
HIGH_52W_MAX_AGE_DAYS = 7
# Endeks için tutulacak işlem günü sayısı ve tam yenileme süresi
INDEX_BARS = 220
INDEX_MAX_AGE_DAYS = 90
# API sayfa başına en fazla 100 bar veriyor
INDEX_MAX_PAGES = 4


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(path: str, params: dict | None = None, url: str | None = None) -> dict:
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    resp = requests.get(
        url or (config.BASE_URL + path),
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


# ---------------------------------------------------------------- fiyat

def fetch_snapshot(ticker: str) -> dict | None:
    """Anlık fiyat (1 istek). Veri yoksa None döner, hata fırlatmaz."""
    try:
        data = _get("/prices/snapshot", {"ticker": ticker})
    except Exception:  # noqa: BLE001 - fallback'e düşülecek
        return None
    snap = data.get("snapshot") or {}
    price = snap.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    t = str(snap.get("time") or "")
    return {"price": float(price), "price_source": "snapshot",
            "price_date": t[:10] if len(t) >= 10 else dt.date.today().isoformat()}


def fetch_last_close(ticker: str) -> dict:
    """Son tamamlanmış işlem gününün kapanışı (1 istek, son 10 günlük günlük barlar)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=10)
    data = _get("/prices", {
        "ticker": ticker, "interval": "day", "interval_multiplier": 1,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    bars = data.get("prices") or []
    if not bars:
        raise ValueError(f"{ticker}: günlük fiyat verisi boş döndü")
    bars.sort(key=lambda b: b["time"])
    last = bars[-1]
    return {"price": float(last["close"]), "price_source": "close",
            "price_date": str(last["time"])[:10]}


def fetch_high_52w(ticker: str) -> dict:
    """52 haftalık zirve (1 istek, 1 yıllık haftalık mumlar), split'e göre düzeltilmiş."""
    end = dt.date.today()
    start = end - dt.timedelta(days=370)
    data = _get("/prices", {
        "ticker": ticker, "interval": "week",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    prices = data.get("prices") or []
    if not prices:
        raise ValueError(f"{ticker}: haftalık fiyat verisi boş döndü")
    prices.sort(key=lambda p: p["time"])

    # Split tespiti: önceki kapanış / sonraki açılış >= 1.8 ise split varsay,
    # split öncesi mumları bu oranla böl (API verisi split'e göre ayarlanmamış).
    factors = [1.0] * len(prices)
    cum = 1.0
    for i in range(len(prices) - 1, 0, -1):
        ratio = prices[i - 1]["close"] / prices[i]["open"] if prices[i]["open"] else 0
        if ratio >= 1.8:
            cum *= ratio
        factors[i - 1] = cum

    return {
        "high_52w": max(p["high"] / f for p, f in zip(prices, factors)),
        "high_52w_fetched_at": _now_iso(),
        "split_adjusted": cum != 1.0,
    }


def fetch_prices(ticker: str, old: dict | None) -> tuple[dict, int]:
    """Fiyat bloğunu günceller. Dönüş: (prices, istek sayısı).

    1) snapshot dene; 2) olmazsa son tamamlanmış gün kapanışı;
    3) 52h zirve cache'i HIGH_52W_MAX_AGE_DAYS'den eskiyse haftalık mumları yenile.
    """
    old = old or {}
    before = REQUEST_COUNT
    quote = fetch_snapshot(ticker) or fetch_last_close(ticker)

    high_ts = old.get("high_52w_fetched_at") or old.get("fetched_at")
    if old.get("high_52w") is not None and age_days(high_ts) <= HIGH_52W_MAX_AGE_DAYS:
        high = {"high_52w": old["high_52w"],
                "high_52w_fetched_at": high_ts,
                "split_adjusted": old.get("split_adjusted", False)}
    else:
        high = fetch_high_52w(ticker)
    # Anlık fiyat haftalık mumların zirvesini aşmış olabilir
    high["high_52w"] = max(high["high_52w"], quote["price"])

    prices = {"fetched_at": _now_iso(), **quote, **high}
    return prices, REQUEST_COUNT - before


# ---------------------------------------------------------------- temel

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


def refresh_ticker(ticker: str, want_price: bool, fundamentals_max_age_days: int,
                   allow_fundamentals: bool = True) -> tuple[dict, int, str | None]:
    """Gerekiyorsa veri çeker, cache'i günceller.

    Dönüş: (cache, kullanılan istek sayısı, hata mesajı veya None)
    Hata olursa eldeki cache ile devam edilir; ticker atlanır, skorlama cache'ten yapılır.
    allow_fundamentals=False ise temel veri bayat olsa bile çekilmez (dry-run).
    """
    cache = load_cache(ticker)
    credits = 0
    error = None

    if want_price:
        try:
            cache["prices"], used = fetch_prices(ticker, cache.get("prices"))
            credits += used
        except Exception as e:  # noqa: BLE001 - tek ticker hatası koşuyu durdurmasın
            error = f"price: {e}"

    fund = cache.get("fundamentals") or {}
    if allow_fundamentals and age_days(fund.get("fetched_at")) > fundamentals_max_age_days:
        try:
            cache["fundamentals"] = fetch_fundamentals(ticker)
            credits += config.FUNDAMENTALS_REQUESTS
        except Exception as e:  # noqa: BLE001
            error = (error + "; " if error else "") + f"fundamentals: {e}"

    if credits:
        save_cache(ticker, cache)
    return cache, credits, error


# ---------------------------------------------------------------- endeks

def _fetch_daily_bars(ticker: str, start: dt.date, end: dt.date, max_pages: int) -> list:
    """Günlük barlar; API 100'erlik sayfalar verir, next_page_url ile ilerlenir."""
    bars = []
    data = _get("/prices", {
        "ticker": ticker, "interval": "day", "interval_multiplier": 1,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    pages = 1
    while True:
        bars.extend(data.get("prices") or [])
        nxt = data.get("next_page_url")
        if not nxt or pages >= max_pages:
            break
        data = _get("", url=nxt)
        pages += 1
    return [{"time": str(b["time"])[:10], "close": float(b["close"])} for b in bars]


def refresh_index(ticker: str) -> tuple[dict, int, str | None]:
    """Endeks günlük kapanışlarını günceller (son INDEX_BARS işlem günü).

    Cache INDEX_MAX_AGE_DAYS'den eskiyse veya yoksa tam çekim (~3 istek),
    aksi halde sadece son bardan bugüne eksik barlar eklenir (1 istek).
    Dönüş: (daily, istek sayısı, hata veya None)
    """
    cache = load_cache(ticker)
    daily = cache.get("daily") or {}
    bars = list(daily.get("bars") or [])
    before = REQUEST_COUNT
    today = dt.date.today()
    try:
        if not bars or age_days(daily.get("fetched_at")) > INDEX_MAX_AGE_DAYS:
            # 220 işlem günü ~ 320 takvim günü
            bars = _fetch_daily_bars(ticker, today - dt.timedelta(days=330), today, INDEX_MAX_PAGES)
        else:
            last_day = dt.date.fromisoformat(bars[-1]["time"])
            new = _fetch_daily_bars(ticker, last_day, today, 2)
            merged = {b["time"]: b for b in bars}
            merged.update({b["time"]: b for b in new})  # son (muhtemelen kısmi) bar güncellenir
            bars = list(merged.values())
    except Exception as e:  # noqa: BLE001
        return daily, REQUEST_COUNT - before, f"index {ticker}: {e}"

    bars.sort(key=lambda b: b["time"])
    bars = bars[-INDEX_BARS:]
    daily = {"fetched_at": _now_iso(), "bars": bars}
    cache["daily"] = daily
    save_cache(ticker, cache)
    return daily, REQUEST_COUNT - before, None
