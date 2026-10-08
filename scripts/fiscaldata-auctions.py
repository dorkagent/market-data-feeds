#!/usr/bin/env python3
"""FiscalData Treasury auction results for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Weekly
rates-regime context: bid-to-cover (demand), high yield, and dealer takedown
share per benchmark tenor. Timely with 10Y at multidecade highs.

Endpoint (keyless, public domain):
  https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query
  (sort=-auction_date; only COMPLETED auctions — auction_date <= today and
  bid_to_cover_ratio present; "null" is a literal string in this API.)

Dealer share = primary_dealer_accepted / (primary + indirect + direct accepted).

Writes:
  hidden_files/fiscaldata-auctions/fiscaldata-auctions-latest.json
  hidden_files/fiscaldata-auctions/fiscaldata-auctions-history.jsonl

Stdout: ALERT lines for weak auctions (bid-to-cover < 2.0 on 10Y/30Y); OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import sys

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "fiscaldata-auctions")

API = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/"
       "accounting/od/auctions_query")
BENCHMARKS = ["4-Week", "8-Week", "13-Week", "17-Week", "26-Week", "52-Week",
              "2-Year", "3-Year", "5-Year", "7-Year", "10-Year", "20-Year",
              "30-Year"]

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Weekly Treasury auction demand; bid-to-cover < 2.0 on long bonds = "
        "weak demand context.")


def fnum(v):
    try:
        return float(v) if v not in (None, "null", "") else None
    except (TypeError, ValueError):
        return None


def fetch():
    r = requests.get(API, params={"sort": "-auction_date", "page[size]": 100,
                                  "page[number]": 1}, timeout=60)
    r.raise_for_status()
    return r.json().get("data", [])


def write(snapshot):
    with open(os.path.join(OUTDIR, "fiscaldata-auctions-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "fiscaldata-auctions-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    today = dt.date.today().isoformat()
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "FiscalData Treasury auctions_query (keyless)",
        "status": "ok",
        "tenors": {},
    }
    try:
        rows = fetch()
        latest = {}
        for r in rows:
            ad = r.get("auction_date") or ""
            if ad > today:
                continue
            btc = fnum(r.get("bid_to_cover_ratio"))
            if btc is None:
                continue
            term = (r.get("security_term") or "").strip()
            key = next((b for b in BENCHMARKS if term.startswith(b)), None)
            if key and key not in latest:
                pd = fnum(r.get("primary_dealer_accepted")) or 0
                ind = fnum(r.get("indirect_bidder_accepted")) or 0
                dr = fnum(r.get("direct_bidder_accepted")) or 0
                tot = pd + ind + dr
                latest[key] = {
                    "auction_date": ad,
                    "security_term": term,
                    "bid_to_cover": btc,
                    "high_yield": fnum(r.get("high_yield")),
                    "dealer_share": round(pd / tot, 3) if tot > 0 else None,
                    "indirect_share": round(ind / tot, 3) if tot > 0 else None,
                }
        snapshot["tenors"] = latest
        if not latest:
            raise RuntimeError("no completed auctions found in latest 100 records")
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: fiscaldata-auctions feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    for tenor in ("10-Year", "30-Year"):
        t = snapshot["tenors"].get(tenor)
        if t and t["bid_to_cover"] < 2.0:
            print(f"ALERT fiscaldata-auctions {tenor}: bid-to-cover {t['bid_to_cover']:.2f} "
                  f"on {t['auction_date']} (weak demand)")
    line = ", ".join(f"{k.split('-')[0]}:{v['bid_to_cover']:.2f}"
                     for k, v in snapshot["tenors"].items())
    print(f"OK fiscaldata-auctions: bid-to-cover by tenor — {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
