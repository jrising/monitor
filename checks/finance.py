"""Market prices."""
from checks import check, http_get, ok, thresholds


@check("quote", required=["symbol"], example="""
  - id: tick-vti
    name: VTI
    group: finance
    check: {type: quote, symbol: VTI}
    schedule: every 1h
""")
def quote(spec):
    """Latest price for a ticker (Yahoo's public chart endpoint; unofficial, may change).
    Options: shares (adds position value), below / above (yellow when crossed)."""
    r = http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{spec['symbol']}",
                 params={"range": "1d", "interval": "1d"}, headers={"User-Agent": "Mozilla/5.0"})
    meta = r.json()["chart"]["result"][0]["meta"]
    price = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    stats = {"price": f"{price:,.2f} {meta.get('currency', '')}".strip(),
             "day": f"{(price / prev - 1) * 100:+.2f}%" if prev else None}
    if spec.get("shares"):
        stats["value"] = f"{price * spec['shares']:,.0f}"
    crossed = thresholds(price, spec, "price")
    if crossed:
        crossed["stats"] = stats
        return crossed
    return ok(stats)
