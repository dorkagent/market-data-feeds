#!/usr/bin/env python3
"""SEC EDGAR full-text keyword sweep for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Weekly radar over
8-K/10-K/10-Q filings for red-flag language on watchlist names + holdings:
guidance cuts, CEO departures, going-concern warnings, restatements, Chapter 11.

Endpoint (keyless, no signup): https://efts.sec.gov/LATEST/search-index
  SEC fair-access rules REQUIRE a descriptive User-Agent (403 without it).
  Courtesy limit ~10 req/sec; this script makes 5.

Watchlist matching: hits carry display_names like "Apple Inc. (AAPL)";
we match the "(TICKER)" token.

Writes:
  hidden_files/edgar-fts/edgar-fts-latest.json
  hidden_files/edgar-fts/edgar-fts-history.jsonl

Stdout: ALERT lines for watchlist hits; OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import re
import sys

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "edgar-fts")

UA = "DorkPaperTrading/1.0 (research; contact dork@agentmail.to)"
API = "https://efts.sec.gov/LATEST/search-index"
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
KEYWORDS = ["guidance cut", "CEO resign", "going concern", "restatement",
            "Chapter 11"]
FORMS = "8-K,10-K,10-Q"
LOOKBACK_DAYS = 7

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Weekly 8-K/10-K/10-Q keyword radar; a hit is a flag for the news pass, "
        "not a trade signal.")


def search(keyword, startdt, enddt):
    r = requests.get(API, params={
        "q": f'"{keyword}"', "dateRange": "custom",
        "startdt": startdt, "enddt": enddt, "forms": FORMS,
    }, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    return r.json()


def watchlist_hit(hit):
    """Return matched watchlist ticker or None."""
    names = " ".join(hit.get("_source", {}).get("display_names", []))
    for t in WATCHLIST:
        if re.search(rf"\({re.escape(t)}\)", names):
            return t
    return None


def write(snapshot):
    with open(os.path.join(OUTDIR, "edgar-fts-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "edgar-fts-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    today = dt.date.today()
    startdt = (today - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    enddt = today.isoformat()
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "SEC EDGAR full-text search (efts.sec.gov, keyless)",
        "status": "ok",
        "window": {"startdt": startdt, "enddt": enddt, "forms": FORMS},
        "keywords": {},
    }
    try:
        for kw in KEYWORDS:
            data = search(kw, startdt, enddt)
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            hits = []
            for h in data.get("hits", {}).get("hits", [])[:50]:
                src = h.get("_source", {})
                t = watchlist_hit(h)
                hits.append({
                    "ticker": t,
                    "company": (src.get("display_names") or ["?"])[0],
                    "form": src.get("form"),
                    "file_date": src.get("file_date"),
                    "watchlist": t is not None,
                })
            snapshot["keywords"][kw] = {
                "total_hits": total,
                "watchlist_hits": [x for x in hits if x["watchlist"]],
                "other_sample": [x for x in hits if not x["watchlist"]][:5],
            }
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: edgar-fts feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    flagged = [(kw, x) for kw, k in snapshot["keywords"].items()
               for x in k["watchlist_hits"]]
    for kw, x in flagged:
        print(f"ALERT edgar-fts {x['ticker']}: '{kw}' in {x['form']} "
              f"({x['company']}) filed {x['file_date']}")
    if not flagged:
        totals = {kw: k["total_hits"] for kw, k in snapshot["keywords"].items()}
        print(f"OK edgar-fts: no watchlist hits in 7d window; totals={totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
