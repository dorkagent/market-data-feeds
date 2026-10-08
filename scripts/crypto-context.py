#!/usr/bin/env python3
"""crypto-context.py — daily crypto context feed for the paper-trading simulator.

Covers the crypto sleeve: BTC, ETH, SOL + meme tier (DOGE/SHIB/PEPE/WIF/BONK/TRUMP).

Three legs:
  1. Fear & Greed index (api.alternative.me/fng) — free, no key.
  2. CoinGecko /coins/markets — uses COINGECKO_API_KEY from env if set
     (CoinGecko Demo API, header x-cg-demo-api-key); otherwise keyless
     public API fallback at a lower rate (10-30/min). One batched call/day,
     well within either budget. Attribution "Powered by CoinGecko" is
     required by their terms and is stamped in every snapshot.
  3. DeFiLlama — free, no key. Stablecoin total supply (with day/week prior
     values from the endpoint itself) + top protocol TVL with 1d/7d changes.

Context layer — informs, never triggers trades alone.

Outputs (feed dir: hidden_files/crypto-context/):
  crypto-context-latest.json    full snapshot
  crypto-context-history.jsonl  one compact line per run

Requires only stdlib + requests. Never hardcodes keys: the CoinGecko demo
key is read from the COINGECKO_API_KEY environment variable only.
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

FEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hidden_files", "crypto-context",
)  # repo-relative (VM copy uses the ~/workspace path)
NOTE = "Context layer — informs, never triggers trades alone."

COINS = [
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("solana", "SOL"),
    ("dogecoin", "DOGE"),
    ("shiba-inu", "SHIB"),
    ("pepe", "PEPE"),
    ("dogwifcoin", "WIF"),
    ("bonk", "BONK"),
    ("official-trump", "TRUMP"),
]

HTTP_TIMEOUT = 30
STABLE_WOW_FLAG_PCT = 2.0      # |week-over-week stablecoin supply change| that counts as "sharp"
PROTOCOL_7D_MOVER_PCT = 20.0   # |7d TVL change| that counts as a major mover
PROTOCOL_1D_MOVER_PCT = 10.0


def get(url, params=None, headers=None):
    return requests.get(url, params=params, headers=headers or {}, timeout=HTTP_TIMEOUT)


def num(x, default=0.0):
    """Cast the many string/None fields these APIs return into a float."""
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- leg 1: Fear & Greed
def fear_greed():
    out = {"status": "dead", "history_31d": [], "flags": []}
    r = get("https://api.alternative.me/fng/", params={"limit": 31})
    r.raise_for_status()
    data = r.json().get("data", [])
    hist = []
    for d in data:
        ts = int(num(d.get("timestamp"), 0))
        hist.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "value": int(num(d.get("value"), 0)),
            "classification": d.get("value_classification", "unknown"),
        })
    if not hist:
        raise ValueError("empty data array from api.alternative.me")
    out.update({
        "status": "live",
        "value": hist[0]["value"],
        "classification": hist[0]["classification"],
        "date": hist[0]["date"],
        "history_31d": hist,
    })
    if len(hist) >= 7:
        out["change_7d"] = hist[0]["value"] - hist[6]["value"]
    v = out["value"]
    if v > 75:
        out["flags"].append(
            {"flag": "extreme_greed_contrarian_bearish",
             "detail": f"Fear & Greed {v} > 75 — crowded-long zone; favor reduced size / tight stops on crypto longs."})
    elif v < 25:
        out["flags"].append(
            {"flag": "extreme_fear_contrarian_bullish",
             "detail": f"Fear & Greed {v} < 25 — capitulation zone; dip entries historically favored, size carefully."})
    return out


# ---------------------------------------------------------------- leg 2: CoinGecko
def coingecko():
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    headers = {"x-cg-demo-api-key": key} if key else {}
    out = {
        "status": "dead",
        "attribution": "Powered by CoinGecko",
        "key_status": "env" if key else "pending (keyless fallback, lower rate)",
        "coins": {},
    }
    r = get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ",".join(cid for cid, _ in COINS),
            "price_change_percentage": "1h,24h,7d",
        },
        headers=headers,
    )
    r.raise_for_status()
    for row in r.json():
        sym = next((s for cid, s in COINS if cid == row.get("id")), row.get("symbol", "?").upper())
        out["coins"][sym] = {
            "price_usd": num(row.get("current_price")),
            "market_cap_usd": num(row.get("market_cap")),
            "chg_1h_pct": num(row.get("price_change_percentage_1h_in_currency")),
            "chg_24h_pct": num(row.get("price_change_percentage_24h_in_currency")),
            "chg_7d_pct": num(row.get("price_change_percentage_7d_in_currency")),
        }
    out["status"] = "live"
    return out


# ---------------------------------------------------------------- leg 3: DeFiLlama
def defillama():
    out = {"status": "dead", "stablecoins": {}, "protocol_tvl_top15": [], "protocol_movers": []}

    # 3a. stablecoin supply — endpoint ships prev-day and prev-week values
    r = get("https://stablecoins.llama.fi/stablecoins", params={"includePrices": "false"})
    r.raise_for_status()
    assets = r.json().get("peggedAssets", [])
    total = sum(num(a.get("circulating", {}).get("peggedUSD")) for a in assets)
    total_prev_day = sum(num(a.get("circulatingPrevDay", {}).get("peggedUSD")) for a in assets)
    total_prev_week = sum(num(a.get("circulatingPrevWeek", {}).get("peggedUSD")) for a in assets)
    top5 = sorted(
        ({"name": a.get("name"), "symbol": a.get("symbol"),
          "circulating_usd": num(a.get("circulating", {}).get("peggedUSD"))}
         for a in assets),
        key=lambda x: x["circulating_usd"], reverse=True,
    )[:5]
    stables = {
        "total_supply_usd": total,
        "wow_pct": (total - total_prev_week) / total_prev_week * 100 if total_prev_week else 0.0,
        "day_pct": (total - total_prev_day) / total_prev_day * 100 if total_prev_day else 0.0,
        "top5": top5,
        "flags": [],
    }
    if abs(stables["wow_pct"]) > STABLE_WOW_FLAG_PCT:
        direction = "expansion" if stables["wow_pct"] > 0 else "contraction"
        stables["flags"].append(
            {"flag": f"stablecoin_supply_sharp_{direction}",
             "detail": (f"Total stablecoin supply {stables['wow_pct']:+.2f}% week-over-week "
                        f"(${total/1e9:,.1f}B) — {'fresh liquidity entering crypto' if stables['wow_pct'] > 0 else 'liquidity leaving crypto'}; "
                        "leading indicator for risk appetite.")})
    out["stablecoins"] = stables

    # 3b. protocol TVL movers
    r = get("https://api.llama.fi/protocols")
    r.raise_for_status()
    protos = sorted(r.json(), key=lambda p: num(p.get("tvl")), reverse=True)[:15]
    top15 = []
    for p in protos:
        c1d, c7d = num(p.get("change_1d")), num(p.get("change_7d"))
        row = {"name": p.get("name"), "tvl_usd": num(p.get("tvl")),
               "change_1d_pct": round(c1d, 2), "change_7d_pct": round(c7d, 2)}
        top15.append(row)
        if abs(c7d) >= PROTOCOL_7D_MOVER_PCT or abs(c1d) >= PROTOCOL_1D_MOVER_PCT:
            out["protocol_movers"].append(row)
    out["protocol_tvl_top15"] = top15
    out["status"] = "live"
    return out


# ---------------------------------------------------------------- main
def main():
    os.makedirs(FEED_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    pt = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))

    snap = {
        "run_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_at_pt": pt.strftime("%Y-%m-%d %H:%M %Z"),
        "note": NOTE,
        "failures": [],
        "flags": [],
    }
    legs = [("fear_greed", fear_greed), ("coingecko", coingecko), ("defillama", defillama)]
    for name, fn in legs:
        try:
            snap[name] = fn()
            snap["flags"].extend(snap[name].get("flags", []))
            snap["flags"].extend(snap[name].get("stablecoins", {}).get("flags", []))
            for m in snap[name].get("protocol_movers", []):
                snap["flags"].append(
                    {"flag": "protocol_tvl_mover",
                     "detail": f"{m['name']}: TVL ${m['tvl_usd']/1e9:,.1f}B, {m['change_7d_pct']:+.1f}% 7d / {m['change_1d_pct']:+.1f}% 1d"})
        except Exception as e:
            snap[name] = {"status": "dead", "error": f"{type(e).__name__}: {e}"}
            snap["failures"].append({"leg": name, "error": f"{type(e).__name__}: {e}"})
            print(f"[FAIL] {name}: {e}", file=sys.stderr)

    with open(os.path.join(FEED_DIR, "crypto-context-latest.json"), "w") as f:
        json.dump(snap, f, indent=2)

    compact = {
        "run_at_utc": snap["run_at_utc"],
        "fear_greed_value": snap.get("fear_greed", {}).get("value"),
        "stablecoin_wow_pct": round(snap.get("defillama", {}).get("stablecoins", {}).get("wow_pct", 0.0), 3),
        "flags": [f["flag"] for f in snap["flags"]],
        "failures": [f["leg"] for f in snap["failures"]],
    }
    with open(os.path.join(FEED_DIR, "crypto-context-history.jsonl"), "a") as f:
        f.write(json.dumps(compact) + "\n")

    # stdout summary for the cron worker to relay
    fg = snap.get("fear_greed", {})
    dl = snap.get("defillama", {})
    print(f"run={snap['run_at_pt']}")
    print(f"fear_greed={fg.get('value')} ({fg.get('classification')}) status={fg.get('status')}")
    st = dl.get("stablecoins", {})
    print(f"stablecoin_supply=${st.get('total_supply_usd', 0)/1e9:,.1f}B wow={st.get('wow_pct', 0):+.2f}% status={dl.get('status')}")
    print(f"coingecko status={snap.get('coingecko', {}).get('status')} key={snap.get('coingecko', {}).get('key_status')}")
    print(f"protocol_movers={len(dl.get('protocol_movers', []))}")
    for fl in snap["flags"]:
        print(f"FLAG {fl['flag']}: {fl['detail']}")
    for fa in snap["failures"]:
        print(f"FAILURE {fa['leg']}: {fa['error']}")
    print("NOTE: " + NOTE)


if __name__ == "__main__":
    main()
