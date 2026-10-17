# invest — yatırım tarama motoru

Financial Datasets API'den fiyat + temel veri çeker, 5 soruluk test skoru, makro rejim ve
portföy risk hesabı üretir. **Hiçbir şey emir vermez, sadece hesaplar.** Panel/UI yok.

## Çalıştırma

```
py engine/run.py                 # tam çalıştırma: fiyat (takvime göre) + bayat temel veri + SPY
py engine/run.py --dry-run       # test koşusu: TÜM fiyatlar + SPY çekilir, temel veri cache'ten (çekilmez)
py engine/run.py --force-prices  # takvimden bağımsız tüm fiyatlar + bayat temel veri + SPY
py engine/run.py --no-api        # hiç API çağrısı yok, sadece cache'ten skor/makro/risk
```

Her koşu sonunda `TOPLAM İSTEK: N (~$X)` yazdırılır. İstek başı ~$0.02.

- `.env` → `FD_API_KEY=...` (bkz. `.env.example`)
- `positions.json` → gerçek pozisyonlar (gitignore'da). Yoksa (örn. CI'da) `risk: null` yazılır; örnek dosya ile hesaplanmaz.
- `data/cache/` repoya commit edilir; CI bu sayede 90 günlük temel veri cache'ini kullanır.

## Zamanlama (CI)

`.github/workflows/run.yml`: cron `45 14 * * 1,3,5` → Pzt/Çar/Cum 14:45 UTC (ABD açılışından
~75 dk sonra). `workflow_dispatch` ile elle de tetiklenir. Koşu sonrası `data/` commit+push edilir.
`FD_API_KEY` repo secret'ı gerekir.

## universe.json

| alan | açıklama |
|---|---|
| `mode` | `normal` / `light` / `paused` (bkz. `engine/config.py` MODES) |
| `cash_usd` | nakit (varsayılan 1500), risk hesabında portföye eklenir |
| `index` | makro endeks listesi, ilki kullanılır (`["SPY"]`) |
| `tier_a`, `tier_b`, `spec` | ticker listeleri; spec için sıkı risk limitleri |
| `setup_prices` | ticker → setup fiyatı (yoksa 52h zirve × 0.8) |
| `theme` | ticker → tema: `ai_infra`, `software`, `consumer`, `health`, `utility`, `defense`, `quantum`, `other` |

## İstek bütçesi

| işlem | istek |
|---|---|
| ticker fiyatı (snapshot) | 1 / ticker / koşu |
| snapshot gelmezse son kapanış (günlük bar) | +1 |
| 52h zirve (haftalık mumlar), 7 günde bir | +1 / ticker |
| temel veri (income ttm + quarterly + balance + cash flow), 90 günde bir | 4 / ticker |
| SPY günlük kapanış: ilk dolum / 90 günde bir tam | 3 |
| SPY artımlı (sadece son barlar) | 1 / koşu |

## Fiyat kaynağı

1. `/prices/snapshot` → `price_source: "snapshot"`, `price_date` = snapshot zamanının günü
2. Gelmezse `/prices` günlük (son 10 gün) → son tamamlanmış barın kapanışı, `price_source: "close"`

52h zirve haftalık mumlardan, split düzeltmeli (önceki kapanış / sonraki açılış ≥ 1.8 → split),
anlık fiyat zirveyi aşarsa zirve = anlık fiyat.

## Makro

- `m1 = 1` ise SPY fiyatı < 200 günlük basit ortalama (son 220 işlem günü cache'te)
- `m2 = 1` ise evrende `pct_from_52w_high <= -20` olan hisse oranı ≥ %30
- `score_partial = m1 + m2`. **m3 (VIX) motorda yok, brifing ekler.**

## Risk (engine/risk.py)

Portföy = pozisyon değerleri (latest.json fiyatlarıyla) + `cash_usd`.

| limit | değer |
|---|---|
| tek hisse | 0.25 |
| tek tema | 0.40 |
| spek toplam | 0.30 |
| tek spek hisse | 0.10 |

`max_new_buy_usd[ticker] = max(0, min(hisse limiti·P − mevcut, 0.40·P − tema toplamı, nakit))`;
spek ticker'larda hisse limiti 0.10 ve ek olarak `0.30·P − spek toplamı` uygulanır.

## data/latest.json şeması

```jsonc
{
  "run_date": "2026-10-17",
  "run_at": "2026-10-17T13:45:00Z",
  "mode": "normal",
  "dry_run": false,
  "tickers_fetched": 38,            // fiyatı bu koşuda çekilen (hatasız) ticker sayısı
  "requests_made": 41,              // toplam HTTP isteği
  "credits_used_estimate": 0.82,    // requests_made × 0.02
  "data_missing": ["..."],          // fiyat veya temel verisi olmayan ticker'lar
  "errors": { "TICKER": "price: ... | fundamentals: ... | index SPY: ..." },

  "macro": {
    "index": "SPY",
    "spy_price": 763.25,
    "spy_ma200": 725.01,
    "m1": 0,                        // 1: SPY < MA200
    "m2": 1,                        // 1: breadth_pct >= 30
    "breadth_pct": 39.5,            // zirveden >= %20 uzak hisse yüzdesi
    "score_partial": 1              // m1 + m2 (m3 VIX brifingde eklenir); veri yoksa null
  },

  "risk": {                         // positions.json yoksa null
    "positions_source": "positions.json",
    "portfolio_usd": 11196.9,
    "positions_usd": 9696.9,
    "cash_usd": 1500.0,
    "spec_weight": 0.0,
    "weights": { "META": 0.6305, "MSFT": 0.2355 },          // ticker → portföy ağırlığı
    "theme_weights": { "software": 0.866 },                  // tema → ağırlık
    "breaches": [                                            // limit ihlalleri
      { "type": "single",      "ticker": "META",   "weight": 0.6305, "limit": 0.25 },
      { "type": "single_spec", "ticker": "QBTS",   "weight": 0.12,   "limit": 0.10 },
      { "type": "theme",       "theme": "software","weight": 0.866,  "limit": 0.40 },
      { "type": "spec_total",  "weight": 0.35, "limit": 0.30 }
    ],
    "max_new_buy_usd": { "META": 0.0, "NVDA": 1500.0, "QBTS": 1119.69 },  // evrendeki her ticker
    "missing_price": [],                                     // fiyatı olmadığı için hesaba girmeyen pozisyonlar
    "limits": { "single": 0.25, "theme": 0.4, "spec_total": 0.3, "spec_single": 0.1 }
  },

  "tickers": [
    {
      "ticker": "NFLX",
      "tier": "tier_a",                 // tier_a | tier_b | spec
      "theme": "consumer",
      "data_missing": false,
      "price": 70.4,
      "price_source": "snapshot",       // snapshot | close
      "price_date": "2026-10-16",
      "pct_from_52w_high": -43.38,
      "pct_from_cost": null,            // positions'ta varsa maliyete göre %
      "questions": { "q1": true, "q2": true, "q3": true, "q4": true, "q5": true },  // true/false/null
      "test_score": 5,                  // true sayısı
      "setup_price": 99.47,
      "distance_to_setup_pct": -29.22,
      "flag": "SETUP"                   // NIKE > SETUP > WATCH > NONE
    }
  ]
}
```

`mode: paused` ise dosya sadece `{"paused": true, "run_date": "..."}` içerir.

### Sorular (engine/score.py)

| soru | kural |
|---|---|
| q1 | TTM gelir > 4 çeyrek önceki TTM |
| q2 | TTM seyreltilmiş EPS > önceki TTM |
| q3 | son çeyrek faaliyet marjı ≥ son 4 çeyrek ortalaması (en az 2 çeyrek) |
| q4 | ROIC = NOPAT / (özkaynak + net borç) > %15 |
| q5 | net borç / EBITDA < 2 (net borç ≤ 0 ise true) |

Bayraklar: `NIKE` (q1 veya q2 false, tez bozulması) > `SETUP` (test_score ≥ 4 ve fiyat ≤ setup)
> `WATCH` (test_score ≥ 4 ve fiyat setup'ın en fazla %10 üstünde) > `NONE`. test_score < 4 ise SETUP/WATCH verilmez.
