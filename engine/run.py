"""fetch + score + macro + risk: data/latest.json üretir.

Kullanım:
  python engine/run.py                 # tam çalıştırma (fiyat + bayat temel veri + SPY)
  python engine/run.py --dry-run       # temel veriyi cache'ten kullanır, yeniden ÇEKMEZ;
                                       # sadece fiyat + SPY çeker, istek sayısını yazdırır
  python engine/run.py --force-prices  # takvimden bağımsız tüm ticker'lar için fiyat çek
  python engine/run.py --no-api        # hiç API çağrısı yok, sadece cache'ten skor/makro/risk

Hiçbir şey emir vermez, sadece hesaplar.
"""
import argparse
import datetime as dt
import json
import sys

import config
import fetch
import risk
import score

# Windows konsolu varsayılan cp1252 kullanabilir; Türkçe karakterler için UTF-8'e geç
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DAY_NAMES = "Pzt Sal Çar Per Cum Cmt Paz".split()


def load_json(path, default=None):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def wants_price_today(tier: str, mode_cfg: dict, today: dt.date) -> bool:
    wd = today.weekday()  # 0=Pzt
    if tier == "tier_a":
        return wd in mode_cfg["tier_a_days"]
    if tier == "tier_b":
        if wd not in mode_cfg["tier_b_days"]:
            return False
        if mode_cfg["tier_b_biweekly"]:
            return today.isocalendar().week % 2 == 0
        return True
    if tier == "spec":
        return wd in mode_cfg["spec_days"]
    return False


def load_positions() -> tuple[dict, str]:
    """positions.json yoksa positions.example.json. Dönüş: ({ticker: pozisyon}, kaynak dosya adı)."""
    for path in (config.POSITIONS_FILE, config.POSITIONS_EXAMPLE_FILE):
        raw = load_json(path)
        if raw is not None:
            return {p["ticker"]: p for p in raw.get("positions", [])}, path.name
    return {}, "none"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="temel veriyi cache'ten kullan (çekme); sadece fiyat + SPY çek")
    parser.add_argument("--force-prices", action="store_true",
                        help="takvimden bağımsız tüm ticker'lar için fiyat çek (ilk dolum/backfill)")
    parser.add_argument("--no-api", action="store_true",
                        help="hiç API çağrısı yapma, sadece cache'ten hesapla")
    args = parser.parse_args()

    universe = load_json(config.UNIVERSE_FILE)
    if universe is None:
        print("HATA: universe.json bulunamadı", file=sys.stderr)
        return 1

    mode = universe.get("mode", "normal")
    if mode not in config.MODES:
        print(f"HATA: bilinmeyen mod '{mode}' (normal/light/paused)", file=sys.stderr)
        return 1

    today = dt.date.today()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    if mode == "paused":
        out = {"paused": True, "run_date": today.isoformat()}
        with open(config.LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print("mod=paused: latest.json yazıldı, API çağrısı yapılmadı")
        return 0

    mode_cfg = config.MODES[mode]
    setup_prices = universe.get("setup_prices", {})
    themes = universe.get("theme", {})
    index_tickers = universe.get("index", [])
    positions, positions_src = load_positions()

    tiers = [("tier_a", universe.get("tier_a", [])),
             ("tier_b", universe.get("tier_b", [])),
             ("spec", universe.get("spec", []))]
    universe_tickers = [t for _, ts in tiers for t in ts]

    use_api = not args.no_api
    allow_fund = use_api and not args.dry_run
    # dry-run bir test koşusudur: takvimden bağımsız tüm fiyatları çeker, temel veriyi çekmez
    force_prices = args.force_prices or args.dry_run

    # Plan: hangi ticker için bugün fiyat çekilecek, temel veri bayat mı?
    plan = []  # (ticker, tier, want_price, want_fundamentals)
    for tier, tickers in tiers:
        want_price = use_api and (force_prices or wants_price_today(tier, mode_cfg, today))
        for t in tickers:
            fund = (fetch.load_cache(t).get("fundamentals") or {})
            stale = fetch.age_days(fund.get("fetched_at")) > mode_cfg["fundamentals_max_age_days"]
            plan.append((t, tier, want_price, allow_fund and stale))

    label = "[dry-run] " if args.dry_run else ("[no-api] " if args.no_api else "")
    print(f"{label}mod={mode}  tarih={today} ({DAY_NAMES[today.weekday()]})  "
          f"pozisyon kaynağı={positions_src}")
    n_price = sum(1 for _, _, wp, _ in plan if wp)
    n_fund = sum(1 for _, _, _, wf in plan if wf)
    print(f"Plan: {n_price} ticker fiyat, {n_fund} ticker temel veri, endeks={index_tickers}")
    if args.dry_run:
        skipped = [t for t, _, _, _ in plan
                   if fetch.age_days((fetch.load_cache(t).get('fundamentals') or {}).get('fetched_at'))
                   > mode_cfg["fundamentals_max_age_days"]]
        if skipped:
            print(f"[dry-run] temel veri bayat ama çekilmiyor: {skipped}")

    if use_api and not config.FD_API_KEY:
        print("HATA: FD_API_KEY tanımlı değil (.env veya ortam değişkeni)", file=sys.stderr)
        return 1

    results = []
    data_missing = []
    errors = {}
    for t, tier, want_price, _ in plan:
        cache, _, error = fetch.refresh_ticker(
            t, want_price, mode_cfg["fundamentals_max_age_days"], allow_fundamentals=allow_fund)
        if error:
            errors[t] = error
        row = score.score_ticker(t, cache, setup_prices, positions)
        row["tier"] = tier
        row["theme"] = themes.get(t, "other")
        if error:
            row["data_missing"] = True
        if row["data_missing"]:
            data_missing.append(t)
        results.append(row)

    # Makro: endeks (SPY) günlük kapanışları
    macro = None
    if index_tickers:
        idx = index_tickers[0]
        if use_api:
            daily, _, err = fetch.refresh_index(idx)
            if err:
                errors[idx] = err
        else:
            daily = fetch.load_cache(idx).get("daily") or {}
        macro = score.compute_macro(daily, results)
        macro["index"] = idx

    # Risk: pozisyon ağırlıkları + alım kapasitesi
    prices = {r["ticker"]: r["price"] for r in results if r["price"] is not None}
    risk_out = risk.compute_risk(positions, prices, themes, universe.get("spec", []),
                                 universe_tickers, universe.get("cash_usd"))
    risk_out["positions_source"] = positions_src

    requests_made = fetch.REQUEST_COUNT
    out = {
        "run_date": today.isoformat(),
        "run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "dry_run": args.dry_run,
        "tickers_fetched": sum(1 for t, _, wp, _ in plan if wp and t not in errors),
        "requests_made": requests_made,
        "credits_used_estimate": round(requests_made * config.COST_PER_REQUEST_USD, 2),
        "data_missing": data_missing,
        "errors": errors,
        "macro": macro,
        "risk": risk_out,
        "tickers": results,
    }
    with open(config.LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    flags = {r["ticker"]: r["flag"] for r in results if r["flag"] != "NONE"}
    sources = {}
    for r in results:
        sources[r["price_source"]] = sources.get(r["price_source"], 0) + 1
    print(f"Tamamlandı: {len(results)} ticker, eksik veri: {len(data_missing)}, "
          f"fiyat kaynağı: {sources}")
    if macro:
        print("Makro:", json.dumps(macro))
    if risk_out["breaches"]:
        print("Risk ihlalleri:", json.dumps(risk_out["breaches"]))
    if flags:
        print("Bayraklar:", json.dumps(flags))
    if errors:
        print("Hatalar:", json.dumps(errors))
    print(f"TOPLAM İSTEK: {requests_made}  (~${out['credits_used_estimate']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
