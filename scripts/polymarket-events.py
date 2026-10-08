#!/usr/bin/env python3
"""Polymarket event-risk feed — macro event probabilities as real-money forecasts.

Free, no auth. Gamma API (https://gamma-api.polymarket.com) for market
metadata/prices; prices embedded as outcomePrices (YES/NO) in market records.

Tracks:
  - Next FOMC meeting event (probabilities of cut/hold/hike outcomes)
  - Next jobs-print market (Sept/Oct unemployment distribution -> modal rate)
  - US recession odds (context), Fed hike/cut count regime (context)

Outputs:
  hidden_files/polymarket/polymarket-latest.json  (tracked probs + flags)
  hidden_files/polymarket/polymarket-history.jsonl (one line per run)

Flags: >10 point day-over-day probability swing on a tracked headline market
= event-risk repricing worth noting ahead of the print. Context layer only:
"informs, never triggers trades alone" (pairs with the no-trade-within-30-min
of FOMC/CPI/jobs-print rule).

std lib + requests only.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://gamma-api.polymarket.com"
HERE = os.path.dirname(os.path.abspath(__file__))
GOAL = os.path.dirname(HERE)  # goals/alpaca-paper-trading-simulator
OUTDIR = os.path.join(GOAL, "hidden_files", "polymarket")
LATEST = os.path.join(OUTDIR, "polymarket-latest.json")
HIST = os.path.join(OUTDIR, "polymarket-history.jsonl")
SWING_THRESHOLD_PTS = 10.0

# Tracked events: {key: event_id}. The jobs-print event resolves then closes;
# the finder below re-discovers the next one.
TRACKED = {
    "fomc_next": 606422,          # Fed Decision in October? (next FOMC)
    "fed_dec": 770450,            # Fed Decision in December? (following meeting)
    "rate_hikes_2026": 626860,    # How many Fed rate hikes in 2026? (regime)
    "recession_2026": 48802,      # US recession by end of 2026? (context)
    "jobs_next": 1120075,           # October Unemployment Rate (next jobs print; ends 2026-11-06)
}


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def get(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def get_event(eid):
    r = get(f"{BASE}/events/{eid}")
    if not r.get("markets"):
        return None
    return r


def find_next_unemployment_event():
    """Re-discover the active unemployment-rate event (each monthly print resolves)."""
    offset = 0
    while offset < 1000:
        evs = get(f"{BASE}/events", params={
            "active": "true", "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"})
        if not evs:
            break
        for e in evs:
            t = (e.get("title") or "").lower()
            if "unemployment rate" in t or ("jobs report" in t):
                markets = e.get("markets") or []
                if markets:
                    return e
        offset += 100
    return None


def summarize_market(m):
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except ValueError:
            prices = []
    return {
        "question": m.get("question"),
        "yes_price": f(prices[0]) if prices else None,
        "no_price": f(prices[1]) if len(prices) > 1 else None,
        "last_trade": f(m.get("lastTradePrice")),
        "volume24h": f(m.get("volume24hr")),
        "volume": f(m.get("volume")),
        "liquidity": f(m.get("liquidity")),
        "end_date": m.get("endDate"),
        "closed": m.get("closed", False),
    }


def summarize_event(e, headline_by="volume24h"):
    markets = [summarize_market(m) for m in (e.get("markets") or [])]
    if headline_by == "yes_price":
        markets.sort(key=lambda x: (x["yes_price"] is not None, x["yes_price"] or 0), reverse=True)
    else:
        markets.sort(key=lambda x: x["volume24h"] or 0, reverse=True)
    headline = markets[0] if markets else None
    return {
        "event_id": e.get("id"),
        "title": e.get("title"),
        "end_date": e.get("endDate"),
        "volume24h": f(e.get("volume24hr")),
        "volume": f(e.get("volume")),
        "n_markets": len(markets),
        "markets": markets,
        "headline": headline,
    }


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    snapshot = {
        "fetched_at": now.isoformat(),
        "feed": "polymarket-events",
        "events": {},
        "flags": [],
        "note": "Context layer — informs, never triggers trades alone.",
    }

    for key, eid in TRACKED.items():
        e = get_event(eid)
        # Monthly print events resolve but keep returning their closed markets;
        # treat a fully-closed event as gone so the finder can re-discover it.
        closed_all = e is not None and bool(e.get("markets")) and all(
            m.get("closed") for m in e.get("markets"))
        if (e is None or closed_all) and key == "jobs_next":
            # monthly print event resolved/closed; re-discover the next one
            e = find_next_unemployment_event()
        if e is None:
            snapshot["events"][key] = {"error": f"event {eid} unavailable (closed or missing)"}
            continue
        # rate_hikes_2026 is a multi-outcome count event: headline = modal outcome
        hby = "yes_price" if key == "rate_hikes_2026" else "volume24h"
        snapshot["events"][key] = summarize_event(e, headline_by=hby)

    # --- Flag logic: >10pt day-over-day swing on tracked headline markets ---
    prev = None
    if os.path.exists(HIST):
        try:
            with open(HIST) as fh:
                lines = [ln for ln in fh if ln.strip()]
            if lines:
                prev = json.loads(lines[-1])
        except Exception:
            prev = None

    if prev:
        prev_probs = {k: v for k, v in prev.get("probs", {}).items()}
        for key, ev in snapshot["events"].items():
            if "error" in ev or not ev.get("headline"):
                continue
            cur = ev["headline"]["yes_price"]
            old = prev_probs.get(key)
            if cur is None or old is None:
                continue
            swing = (cur - old) * 100.0
            if abs(swing) >= SWING_THRESHOLD_PTS:
                snapshot["flags"].append({
                    "key": key,
                    "event": ev["title"],
                    "market": ev["headline"]["question"],
                    "prev_prob_pct": round(old * 100, 1),
                    "cur_prob_pct": round(cur * 100, 1),
                    "swing_pts": round(swing, 1),
                    "meaning": "event-risk repricing worth noting ahead of the print; pairs with the no-trade-within-30-min-of-FOMC/CPI/jobs-print blackout rule (context, not a trigger).",
                })

    # Record headline probabilities for next run's day-over-day comparison
    snapshot["probs"] = {
        key: ev["headline"]["yes_price"]
        for key, ev in snapshot["events"].items()
        if "error" not in ev and ev.get("headline") and ev["headline"].get("yes_price") is not None
    }

    with open(LATEST, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    with open(HIST, "a") as fh:
        fh.write(json.dumps({
            "fetched_at": snapshot["fetched_at"],
            "probs": snapshot["probs"],
        }) + "\n")

    # Human-readable console summary (cron stays quiet unless flags or failures)
    print(f"[{snapshot['fetched_at']}] polymarket-events feed: {len(snapshot['probs'])} tracked markets")
    for key, ev in snapshot["events"].items():
        if "error" in ev:
            print(f"  WARN {key}: {ev['error']}")
            continue
        h = ev["headline"]
        print(f"  {key}: {h['question']} — YES {h['yes_price']*100:.1f}% (vol24h ${h['volume24h']:,.0f})")
    for fl in snapshot["flags"]:
        print(f"  FLAG {fl['key']}: {fl['swing_pts']:+.1f} pts → {fl['cur_prob_pct']:.1f}% — {fl['market']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
