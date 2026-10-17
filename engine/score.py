"""Cache'teki ham veriden ticker başına skor hesaplar.

5 soruluk test (her biri true/false/null):
  q1 gelir büyüyor      : TTM gelir > önceki TTM (4 çeyrek önce biten TTM)
  q2 EPS büyüyor        : TTM seyreltilmiş EPS > önceki TTM
  q3 marj trendi        : son çeyrek faaliyet marjı >= son 4 çeyrek ortalaması
  q4 ROIC > %15         : NOPAT / (özkaynak + net borç)
  q5 kaldıraç           : net borç / EBITDA < 2
"""


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _ttm_pair(income_ttm: list) -> tuple[dict | None, dict | None]:
    """Güncel TTM ve 4 çeyrek önceki TTM (yoksa eldeki en eski)."""
    if not income_ttm:
        return None, None
    cur = income_ttm[0]
    prev = income_ttm[4] if len(income_ttm) >= 5 else (income_ttm[-1] if len(income_ttm) >= 2 else None)
    return cur, prev


def _eps(stmt: dict):
    return _num(stmt.get("earnings_per_share_diluted")) or _num(stmt.get("earnings_per_share"))


def _questions(fund: dict) -> dict:
    q = {"q1": None, "q2": None, "q3": None, "q4": None, "q5": None}
    cur, prev = _ttm_pair(fund.get("income_ttm") or [])

    # q1 / q2: büyüme
    if cur and prev:
        rev_c, rev_p = _num(cur.get("revenue")), _num(prev.get("revenue"))
        if rev_c is not None and rev_p is not None:
            q["q1"] = rev_c > rev_p
        eps_c, eps_p = _eps(cur), _eps(prev)
        if eps_c is not None and eps_p is not None:
            q["q2"] = eps_c > eps_p

    # q3: son çeyrek faaliyet marjı >= son 4 çeyrek ortalaması
    # (API çoğu ticker için 4'ten az çeyrek veriyor; en az 2 çeyrek varsa eldekiyle hesapla)
    margins = []
    for stmt in (fund.get("income_quarterly") or [])[:4]:
        rev, op = _num(stmt.get("revenue")), _num(stmt.get("operating_income"))
        if rev and op is not None:
            margins.append(op / rev)
    if len(margins) >= 2:
        q["q3"] = margins[0] >= sum(margins) / len(margins)

    bal = fund.get("balance") or {}
    equity = _num(bal.get("shareholders_equity"))
    total_debt = _num(bal.get("total_debt"))
    cash = _num(bal.get("cash_and_equivalents"))
    net_debt = (total_debt - cash) if (total_debt is not None and cash is not None) else None

    # q4: ROIC = NOPAT / (özkaynak + net borç)
    if cur and equity is not None and net_debt is not None:
        op_inc = _num(cur.get("operating_income")) or _num(cur.get("ebit"))
        tax = _num(cur.get("income_tax_expense"))
        net_inc = _num(cur.get("net_income"))
        if op_inc is not None and tax is not None and net_inc is not None:
            pretax = net_inc + tax
            tax_rate = min(max(tax / pretax, 0.0), 1.0) if pretax > 0 else 0.0
            nopat = op_inc * (1 - tax_rate)
            invested = equity + net_debt
            if invested > 0:
                q["q4"] = (nopat / invested) > 0.15

    # q5: net borç / EBITDA < 2 (EBITDA = EBIT + amortisman)
    if cur and net_debt is not None:
        ebit = _num(cur.get("ebit")) or _num(cur.get("operating_income"))
        cf = fund.get("cash_flow_ttm") or {}
        dna = _num(cf.get("depreciation_and_amortization")) or 0
        if ebit is not None:
            ebitda = ebit + dna
            if net_debt <= 0:
                q["q5"] = True
            elif ebitda > 0:
                q["q5"] = (net_debt / ebitda) < 2

    return q


def score_ticker(ticker: str, cache: dict, setup_prices: dict, positions: dict) -> dict:
    """Ticker başına skor sözlüğü. Fiyat verisi yoksa data_missing=true."""
    prices = cache.get("prices") or {}
    fund = cache.get("fundamentals") or {}
    price = _num(prices.get("price"))
    high_52w = _num(prices.get("high_52w"))

    out = {
        "ticker": ticker,
        "data_missing": False,
        "price": price,
        "pct_from_52w_high": None,
        "pct_from_cost": None,
        "questions": {"q1": None, "q2": None, "q3": None, "q4": None, "q5": None},
        "test_score": 0,
        "setup_price": None,
        "distance_to_setup_pct": None,
        "flag": "NONE",
    }

    if price is None or high_52w is None:
        out["data_missing"] = True
        return out
    if not fund:
        out["data_missing"] = True

    out["pct_from_52w_high"] = round((price - high_52w) / high_52w * 100, 2)

    pos = positions.get(ticker)
    if pos and _num(pos.get("cost_basis")):
        out["pct_from_cost"] = round((price - pos["cost_basis"]) / pos["cost_basis"] * 100, 2)

    q = _questions(fund)
    out["questions"] = q
    out["test_score"] = sum(1 for v in q.values() if v is True)

    setup = _num(setup_prices.get(ticker)) or round(high_52w * 0.8, 2)
    out["setup_price"] = setup
    out["distance_to_setup_pct"] = round((price - setup) / setup * 100, 2)

    # Bayrak önceliği: NIKE (tez bozulması) > SETUP > WATCH > NONE
    if q["q1"] is False or q["q2"] is False:
        out["flag"] = "NIKE"
    elif out["test_score"] >= 4 and price <= setup:
        out["flag"] = "SETUP"
    elif 0 < out["distance_to_setup_pct"] <= 10:
        out["flag"] = "WATCH"

    return out
