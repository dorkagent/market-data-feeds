#!/usr/bin/env python3
"""Wikipedia pageviews retail-attention proxy for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Daily pageviews
per company article over a 7-day window; attention spikes front-run retail
flows. ~1-day lag, no key, no signup.

Endpoint: https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/
  /en.wikipedia/all-access/user/{article}/daily/{yyyymmdd}/{yyyymmdd}
  Descriptive User-Agent requested by Wikimedia.

Flag: latest-day views > 2x the 7-day median (attention spike context).
History (jsonl) lets later runs use a longer baseline.

Writes:
  hidden_files/wiki-pageviews/wiki-pageviews-latest.json
  hidden_files/wiki-pageviews/wiki-pageviews-history.jsonl

Stdout: ALERT lines for attention spikes; OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import statistics
import sys
import urllib.parse

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "wiki-pageviews")

UA = "DorkPaperTrading/1.0 (research; contact dork@agentmail.to)"
API = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
       "en.wikipedia/all-access/user/{article}/daily/{start}/{end}")
ARTICLES = {"SPY": "SPDR_S%26P_500_ETF_Trust", "QQQ": "Invesco_QQQ",
            "NVDA": "Nvidia", "AAPL": "Apple_Inc.", "MSFT": "Microsoft"}
WINDOW_DAYS = 7
SPIKE_MULT = 2.0

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Retail-attention proxy; spikes are context, not signals.")


def fetch(article, start, end):
    r = requests.get(API.format(article=article, start=start, end=end),
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def write(snapshot):
    with open(os.path.join(OUTDIR, "wiki-pageviews-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "wiki-pageviews-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    today = dt.date.today()
    start = (today - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    end = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "Wikimedia pageviews API (keyless)",
        "status": "ok",
        "window": {"start": start, "end": end},
        "tickers": {},
    }
    try:
        for ticker, article in ARTICLES.items():
            items = fetch(article, start, end)
            views = [(x["timestamp"][:8], x.get("views", 0)) for x in items]
            vals = [v for _, v in views]
            entry = {"article": urllib.parse.unquote(article),
                     "daily": {d: v for d, v in views}}
            if vals:
                med = statistics.median(vals)
                entry["median_7d"] = med
                entry["latest"] = vals[-1]
                entry["latest_date"] = views[-1][0]
                entry["spike_mult"] = round(vals[-1] / med, 2) if med else None
            else:
                entry["error"] = "no data"
            snapshot["tickers"][ticker] = entry
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: wiki-pageviews feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    for ticker, e in snapshot["tickers"].items():
        sm = e.get("spike_mult")
        if sm and sm >= SPIKE_MULT:
            print(f"ALERT wiki-pageviews {ticker}: {e['latest']:,} views on "
                  f"{e['latest_date']} = {sm}x 7d median (attention spike)")
    line = ", ".join(f"{t}:{e.get('latest', '?')}" for t, e in snapshot["tickers"].items())
    print(f"OK wiki-pageviews: latest daily views — {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
