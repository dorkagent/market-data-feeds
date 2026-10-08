#!/usr/bin/env python3
"""OpenInsider cluster buys for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Pre-filtered
insider cluster buys (the "Ins" column = distinct insider count): direct
smart-money corroboration for the Form 4 feed. Cluster = 2+ insiders buying.

Source (free, no key — HTML scrape):
  http://openinsider.com/latest-cluster-buys
  Table columns: Filing Date | Trade Date | Ticker | Company Name | Industry |
  Ins | Trade Type | Price | Qty | Owned | ΔOwn | Value | 1d | 1w | 1m | 6m
  Ticker cell carries a tooltip link: extract the <a> text.
  Browser UA required. The data table is located by its header, not position.

Writes:
  hidden_files/openinsider/openinsider-latest.json
  hidden_files/openinsider/openinsider-history.jsonl

Stdout: ALERT lines for watchlist cluster buys; OK otherwise.
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
OUTDIR = os.path.join(BASE, "hidden_files", "openinsider")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
URL = "http://openinsider.com/latest-cluster-buys"
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
MIN_CLUSTER = 2

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Insider cluster buys corroborate the Form 4 feed; a cluster is "
        "context, never a trade signal.")


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def fetch_rows():
    r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    html = r.text
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.I | re.S)
    target = None
    for t in tables:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.I | re.S)
        if not rows:
            continue
        hdr = [strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>",
                                                 rows[0], re.S)]
        if "Ticker" in hdr and "Trade Date" in hdr:
            target = (hdr, rows[1:])
            break
    if not target:
        raise RuntimeError("cluster-buys table not found (layout changed?)")
    hdr, rows = target
    idx = {h: i for i, h in enumerate(hdr)}
    out = []
    for row in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        if len(cells) < len(hdr):
            continue
        tick_cell = cells[idx["Ticker"]]
        m = re.findall(r">([A-Z0-9.\-^]{1,8})</a>", tick_cell)
        ticker = m[-1] if m else strip_tags(tick_cell)
        try:
            ins = int(strip_tags(cells[idx["Ins"]]) or 0)
        except ValueError:
            ins = 0
        if ins < MIN_CLUSTER:
            continue
        out.append({
            "ticker": ticker,
            "company": strip_tags(cells[idx["Company Name"]])[:60],
            "insiders": ins,
            "trade_date": strip_tags(cells[idx["Trade Date"]]),
            "filing_date": strip_tags(cells[idx["Filing Date"]]),
            "trade_type": strip_tags(cells[idx["Trade Type"]]),
            "price": strip_tags(cells[idx["Price"]]),
            "value": strip_tags(cells[idx["Value"]]),
            "watchlist": ticker in WATCHLIST,
        })
    return out


def write(snapshot):
    with open(os.path.join(OUTDIR, "openinsider-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "openinsider-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "OpenInsider latest-cluster-buys (keyless HTML scrape)",
        "status": "ok",
        "clusters": [],
    }
    try:
        snapshot["clusters"] = fetch_rows()
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: openinsider feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    wl = [c for c in snapshot["clusters"] if c["watchlist"]]
    for c in wl:
        print(f"ALERT openinsider {c['ticker']}: {c['insiders']}-insider cluster buy "
              f"{c['trade_type']} {c['value']} on {c['trade_date']} ({c['company']})")
    print(f"OK openinsider: {len(snapshot['clusters'])} cluster buys scraped; "
          f"{len(wl)} on watchlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
