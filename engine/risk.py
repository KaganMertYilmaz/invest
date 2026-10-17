"""Portföy risk hesabı: ağırlıklar, tema ağırlıkları, limit ihlalleri, alım kapasitesi.

Hiçbir şey emir vermez, sadece hesaplar.

Limitler (portföy = pozisyon değeri + nakit):
  tek hisse        <= 0.25
  tek tema         <= 0.40
  spek toplam      <= 0.30
  tek spek hisse   <= 0.10

max_new_buy_usd[ticker] = min(hisse limiti - mevcut, tema limiti - tema toplamı, nakit)
  spek ticker için hisse limiti 0.10, ayrıca spek toplam limiti 0.30 da uygulanır.
"""

LIMIT_SINGLE = 0.25
LIMIT_THEME = 0.40
LIMIT_SPEC_TOTAL = 0.30
LIMIT_SPEC_SINGLE = 0.10
DEFAULT_CASH_USD = 1500.0


def _num(x):
    return float(x) if isinstance(x, (int, float)) else None


def compute_risk(positions: dict, prices: dict, themes: dict, spec: list,
                 universe_tickers: list, cash_usd: float | None) -> dict:
    """
    positions: {ticker: {"shares": n, "cost_basis": c}}
    prices:    {ticker: fiyat} (latest.json'daki güncel fiyatlar)
    themes:    {ticker: tema}
    spec:      spekülatif ticker listesi
    """
    cash = _num(cash_usd)
    if cash is None:
        cash = DEFAULT_CASH_USD
    spec_set = set(spec)

    values = {}
    missing_price = []
    for t, pos in positions.items():
        shares = _num(pos.get("shares"))
        price = _num(prices.get(t))
        if shares is None or shares <= 0:
            continue
        if price is None:
            missing_price.append(t)
            continue
        values[t] = shares * price

    positions_value = sum(values.values())
    portfolio = positions_value + cash

    weights = {}
    theme_weights = {}
    spec_weight = 0.0
    if portfolio > 0:
        for t, v in values.items():
            w = v / portfolio
            weights[t] = round(w, 4)
            theme = themes.get(t, "other")
            theme_weights[theme] = theme_weights.get(theme, 0.0) + w
            if t in spec_set:
                spec_weight += w
    theme_weights = {k: round(v, 4) for k, v in sorted(theme_weights.items())}

    breaches = []
    for t, w in weights.items():
        if t in spec_set and w > LIMIT_SPEC_SINGLE:
            breaches.append({"type": "single_spec", "ticker": t,
                             "weight": w, "limit": LIMIT_SPEC_SINGLE})
        elif w > LIMIT_SINGLE:
            breaches.append({"type": "single", "ticker": t,
                             "weight": w, "limit": LIMIT_SINGLE})
    for theme, w in theme_weights.items():
        if w > LIMIT_THEME:
            breaches.append({"type": "theme", "theme": theme,
                             "weight": w, "limit": LIMIT_THEME})
    if spec_weight > LIMIT_SPEC_TOTAL:
        breaches.append({"type": "spec_total", "weight": round(spec_weight, 4),
                         "limit": LIMIT_SPEC_TOTAL})

    max_new_buy = {}
    for t in universe_tickers:
        current = values.get(t, 0.0)
        theme = themes.get(t, "other")
        theme_val = theme_weights.get(theme, 0.0) * portfolio
        single_limit = LIMIT_SPEC_SINGLE if t in spec_set else LIMIT_SINGLE
        caps = [
            single_limit * portfolio - current,
            LIMIT_THEME * portfolio - theme_val,
            cash,
        ]
        if t in spec_set:
            caps.append(LIMIT_SPEC_TOTAL * portfolio - spec_weight * portfolio)
        max_new_buy[t] = round(max(0.0, min(caps)), 2)

    return {
        "portfolio_usd": round(portfolio, 2),
        "positions_usd": round(positions_value, 2),
        "cash_usd": round(cash, 2),
        "spec_weight": round(spec_weight, 4),
        "weights": weights,
        "theme_weights": theme_weights,
        "breaches": breaches,
        "max_new_buy_usd": max_new_buy,
        "missing_price": missing_price,
        "limits": {"single": LIMIT_SINGLE, "theme": LIMIT_THEME,
                   "spec_total": LIMIT_SPEC_TOTAL, "spec_single": LIMIT_SPEC_SINGLE},
    }
