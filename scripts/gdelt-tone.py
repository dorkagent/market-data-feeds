#!/usr/bin/env python3
"""GDELT news tone/volume per watchlist ticker for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. News volume
(attention) + average tone (-10..+10) time series per ticker over 7 days.
Upgrades the news pass beyond Alpaca headlines.

Endpoint (keyless, no signup): https://api.gdeltproject.org/api/v2/doc/doc
  modes: timelinetone (avg tone series), timelinevol (volume series).

HARD PACING (egress IP is throttled by GDELT — 429s observed even on single
gentle probes 2026-10-01):
  - max 1 request per 15 seconds
  - aggressive disk cache: per (ticker, mode) JSON, 24h TTL — a daily cron
    therefore usually reuses cache and makes ZERO requests
  - on 429/any error: keep stale cache, mark status "throttled", exit 0
    (degrade silently — this feed must never break the scan)

Writes:
  hidden_files/gdelt-tone/gdelt-tone-latest.json
  hidden_files/gdelt-tone/gdelt-tone-history.jsonl
  hidden_files/gdelt-tone/cache/<ticker>_<mode>.json

Stdout: ALERT lines for tone/volume extremes; OK otherwise.
Exit 0 always (failures degrade to throttled/error snapshot, never crash).
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "gdelt-tone")
CACHEDIR = os.path.join(OUTDIR, "cache")

API = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERIES = {"SPY": "SPDR S&P 500", "QQQ": "Invesco QQQ", "NVDA": "Nvidia",
           "AAPL": "Apple Inc", "MSFT": "Microsoft"}
MODES = ["timelinetone", "timelinevol"]
PACE_SECONDS = 15
CACHE_TTL_HOURS = 24

NOTE = ("Context layer — informs, never triggers trades alone. "
        "GDELT news tone/volume; throttled from this VM — cache-first, "
        "degrades silently on 429.")


def cache_path(ticker, mode):
    return os.path.join(CACHEDIR, f"{ticker}_{mode}.json")


def cache_get(ticker, mode):
    p = cache_path(ticker, mode)
    if not os.path.exists(p):
        return None, True
    try:
        with open(p) as f:
            d = json.load(f)
        age_h = (dt.datetime.now(dt.timezone.utc) -
                 dt.datetime.fromisoformat(d["fetched_utc"])).total_seconds() / 3600
        return d, age_h > CACHE_TTL_HOURS
    except (json.JSONDecodeError, KeyError, ValueError):
        return None, True


def cache_put(ticker, mode, payload):
    with open(cache_path(ticker, mode), "w") as f:
        json.dump({"fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "payload": payload}, f)


def fetch_series(ticker, mode):
    q = QUERIES[ticker]
    r = requests.get(API, params={
        "query": q, "mode": mode, "format": "json", "timespan": "7d",
        "sort": "datedesc",
    }, headers={"User-Agent": "DorkPaperTrading/1.0 (research; contact dork@agentmail.to)"},
        timeout=45)
    if r.status_code == 429:
        raise RuntimeError("throttled (429)")
    r.raise_for_status()
    return r.json()


def summarize(payload, mode):
    tl = payload.get("timeline", [{}])[0].get("data", [])
    if mode == "timelinetone":
        vals = [float(x.get("value", 0)) for x in tl if x.get("value") is not None]
    else:
        vals = [float(x.get("value", 0)) for x in tl if x.get("value") is not None]
    if not vals:
        return {"points": 0}
    return {"points": len(vals), "latest": round(vals[-1], 2),
            "avg_7d": round(sum(vals) / len(vals), 2),
            "min_7d": round(min(vals), 2), "max_7d": round(max(vals), 2)}


def write(snapshot):
    with open(os.path.join(OUTDIR, "gdelt-tone-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "gdelt-tone-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(CACHEDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "GDELT DOC 2.1 API (keyless, cache-first, 15s pacing)",
        "status": "ok",
        "tickers": {},
        "throttled": False,
    }
    for ticker in QUERIES:
        entry = {}
        for mode in MODES:
            cached, stale = cache_get(ticker, mode)
            if cached and not stale:
                entry[mode] = summarize(cached["payload"], mode)
                entry[mode]["from_cache"] = True
                continue
            try:
                time.sleep(PACE_SECONDS)
                payload = fetch_series(ticker, mode)
                cache_put(ticker, mode, payload)
                entry[mode] = summarize(payload, mode)
                entry[mode]["from_cache"] = False
            except Exception as e:
                snapshot["throttled"] = "429" in str(e)
                if cached:
                    entry[mode] = summarize(cached["payload"], mode)
                    entry[mode]["from_cache"] = True
                    entry[mode]["stale"] = True
                else:
                    entry[mode] = {"error": str(e)[:120]}
        snapshot["tickers"][ticker] = entry

    if all("error" in m for t in snapshot["tickers"].values()
           for m in t.values() if isinstance(m, dict)):
        snapshot["status"] = "throttled" if snapshot["throttled"] else "error"
    write(snapshot)

    for ticker, e in snapshot["tickers"].items():
        tone = e.get("timelinetone", {})
        vol = e.get("timelinevol", {})
        if tone.get("avg_7d") is not None and tone["avg_7d"] <= -2.0:
            print(f"ALERT gdelt-tone {ticker}: avg news tone {tone['avg_7d']} over 7d (negative)")
        elif tone.get("points"):
            print(f"OK gdelt-tone {ticker}: tone avg {tone['avg_7d']} "
                  f"(latest {tone['latest']}), vol avg {vol.get('avg_7d')}")
    if snapshot["status"] != "ok":
        print(f"OK gdelt-tone: degraded ({snapshot['status']}) — serving cache, will retry next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
