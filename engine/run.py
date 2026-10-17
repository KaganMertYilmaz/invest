"""fetch + score: data/latest.json üretir.

Kullanım:
  python engine/run.py             # gerçek çalıştırma (API çağrıları yapar)
  python engine/run.py --dry-run   # hiç API çağrısı yapmaz, çekme planını yazdırır
"""
import argparse
import datetime as dt
import json
import sys

import config
import fetch
import score

# Windows konsolu varsayılan cp1252 kullanabilir; Türkçe karakterler için UTF-8'e geç
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="API çağrısı yapmadan çekme planını ve skor iskeletini göster")
    parser.add_argument("--force-prices", action="store_true",
                        help="takvimden bağımsız tüm ticker'lar için fiyat çek (ilk dolum/backfill)")
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

    if mode == "paused":
        out = {"paused": True, "run_date": today.isoformat()}
        if args.dry_run:
            print("[dry-run] mod=paused: hiç API çağrısı yok, latest.json'a paused=true yazılırdı")
            print(json.dumps(out, indent=2))
            return 0
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print("mod=paused: latest.json yazıldı, API çağrısı yapılmadı")
        return 0

    mode_cfg = config.MODES[mode]
    setup_prices = universe.get("setup_prices", {})
    positions_raw = load_json(config.POSITIONS_FILE, {"positions": []})
    positions = {p["ticker"]: p for p in positions_raw.get("positions", [])}

    tiers = [("tier_a", universe.get("tier_a", [])),
             ("tier_b", universe.get("tier_b", [])),
             ("spec", universe.get("spec", []))]

    # Plan: hangi ticker için bugün fiyat çekilecek, temel veri bayat mı?
    plan = []  # (ticker, tier, want_price, want_fundamentals)
    for tier, tickers in tiers:
        want_price = args.force_prices or wants_price_today(tier, mode_cfg, today)
        for t in tickers:
            fund = (fetch.load_cache(t).get("fundamentals") or {})
            want_fund = fetch.age_days(fund.get("fetched_at")) > mode_cfg["fundamentals_max_age_days"]
            plan.append((t, tier, want_price, want_fund))

    est_requests = sum((1 if wp else 0) + (config.FUNDAMENTALS_REQUESTS if wf else 0)
                       for _, _, wp, wf in plan)

    if args.dry_run:
        print(f"[dry-run] mod={mode}  tarih={today} ({'Pzt Sal Çar Per Cum Cmt Paz'.split()[today.weekday()]})")
        print(f"{'TICKER':8} {'TIER':7} {'FİYAT':6} {'TEMEL':6}")
        for t, tier, wp, wf in plan:
            print(f"{t:8} {tier:7} {'evet' if wp else '-':6} {'evet' if wf else '-':6}")
        print(f"\nTahmini istek: {est_requests}  (~${est_requests * config.COST_PER_REQUEST_USD:.2f})")
        # Skorlama kodunu mevcut cache ile dene (API'siz), dosya yazma
        sample = [score.score_ticker(t, fetch.load_cache(t), setup_prices, positions)
                  for t, _, _, _ in plan[:3]]
        print("\nSkor iskeleti örneği (cache'ten, API'siz):")
        print(json.dumps(sample, indent=2))
        print("\n[dry-run] Hiç API çağrısı yapılmadı, hiçbir dosya yazılmadı.")
        return 0

    if not config.FD_API_KEY:
        print("HATA: FD_API_KEY tanımlı değil (.env veya ortam değişkeni)", file=sys.stderr)
        return 1

    credits_used = 0
    results = []
    data_missing = []
    errors = {}
    for t, tier, want_price, _ in plan:
        cache, credits, error = fetch.refresh_ticker(
            t, want_price, mode_cfg["fundamentals_max_age_days"])
        credits_used += credits
        if error:
            errors[t] = error
        row = score.score_ticker(t, cache, setup_prices, positions)
        row["tier"] = tier
        if error:
            row["data_missing"] = True
        if row["data_missing"]:
            data_missing.append(t)
        results.append(row)

    out = {
        "run_date": today.isoformat(),
        "mode": mode,
        "tickers_fetched": sum(1 for t, _, wp, _ in plan if wp and t not in errors),
        "credits_used_estimate": round(credits_used * config.COST_PER_REQUEST_USD, 2),
        "requests_made": credits_used,
        "data_missing": data_missing,
        "errors": errors,
        "tickers": results,
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    flags = {r["ticker"]: r["flag"] for r in results if r["flag"] != "NONE"}
    print(f"Tamamlandı: {len(results)} ticker, {credits_used} istek (~${out['credits_used_estimate']}), "
          f"eksik veri: {len(data_missing)}")
    if flags:
        print("Bayraklar:", json.dumps(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
