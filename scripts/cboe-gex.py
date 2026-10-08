#!/usr/bin/env python3
"""CBOE delayed options-chain gamma exposure (GEX) feed for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Dealer-positioning
regime: gamma walls (strikes where dealers must hedge most aggressively),
zero-gamma flip estimate (level where dealer hedging switches from
dampening to amplifying moves), and max pain. ~15-min delayed chain data,
regenerated ~60s during market hours; OI publishes once daily via OCC.

Endpoint (keyless, no signup):
  https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json
  (_SPX, _NDX for indexes; SPY, QQQ for ETFs; browser UA required; 307 -> cdn-api)

Method (standard retail-dashboard simplification, stated plainly):
  GEX_$ per contract = gamma * OI * 100 * spot^2 * 0.01
  Net GEX per strike = sum(call GEX) - sum(put GEX)
  (assumes dealers are net short calls / net long puts — an approximation,
  not a measured positioning book).
  Flip estimate = strike where cumulative net GEX (ascending) crosses zero.
  Max pain = strike minimizing total intrinsic payout of all OI.
Only expiries within 45 days are included (near-term dealer hedging matters most).

Writes:
  hidden_files/cboe-gex/cboe-gex-latest.json
  hidden_files/cboe-gex/cboe-gex-history.jsonl

Stdout: ALERT lines for notable regime readings; OK otherwise.
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
OUTDIR = os.path.join(BASE, "hidden_files", "cboe-gex")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
SYMBOLS = ["_SPX", "_NDX", "SPY", "QQQ"]
EXPIRY_WINDOW_DAYS = 45

NOTE = ("Context layer — informs, never triggers trades alone. "
        "~15-min delayed; OI updates once daily. GEX uses the dealer-short-calls/"
        "long-puts approximation.")


def fetch_chain(sym):
    r = requests.get(URL.format(sym=sym), headers={"User-Agent": UA},
                     timeout=180)
    r.raise_for_status()
    return r.json()


def parse_occ(occ):
    """SPX261016C00200000 -> (date(2026,10,16), 'C', 2000.0)."""
    try:
        cp = occ[-9]
        if cp not in ("C", "P"):
            return None
        exp = dt.date(2000 + int(occ[-15:-9][:2]), int(occ[-15:-9][2:4]),
                      int(occ[-15:-9][4:6]))
        strike = int(occ[-8:]) / 1000.0
        return exp, cp, strike
    except (ValueError, IndexError):
        return None


def analyze(payload):
    data = payload.get("data", {})
    spot = data.get("current_price")
    ts = payload.get("timestamp") or data.get("timestamp")
    contracts = data.get("options", [])
    today = dt.date.today()
    per_strike = {}
    for c in contracts:
        parsed = parse_occ(c.get("option", ""))
        if not parsed:
            continue
        exp, cp, strike = parsed
        if not (0 <= (exp - today).days <= EXPIRY_WINDOW_DAYS):
            continue
        try:
            gamma = float(c.get("gamma") or 0)
            oi = float(c.get("open_interest") or 0)
        except (TypeError, ValueError):
            continue
        if gamma <= 0 or oi <= 0 or not spot:
            continue
        gex = gamma * oi * 100 * (spot ** 2) * 0.01
        acc = per_strike.setdefault(strike, {"call": 0.0, "put": 0.0})
        acc["call" if cp == "C" else "put"] += gex

    strikes = sorted(per_strike)
    net = {s: per_strike[s]["call"] - per_strike[s]["put"] for s in strikes}
    total_call = sum(per_strike[s]["call"] for s in strikes) / 1e9
    total_put = sum(per_strike[s]["put"] for s in strikes) / 1e9

    # aggregate raw OI per strike for max pain
    oi_call, oi_put = {}, {}
    for c in contracts:
        parsed = parse_occ(c.get("option", ""))
        if not parsed:
            continue
        exp, cp, ks = parsed
        if not (0 <= (exp - today).days <= EXPIRY_WINDOW_DAYS):
            continue
        try:
            oi = float(c.get("open_interest") or 0)
        except (TypeError, ValueError):
            continue
        if oi <= 0:
            continue
        d = oi_call if cp == "C" else oi_put
        d[ks] = d.get(ks, 0.0) + oi

    # zero-gamma flip: cumulative net GEX crossing zero, nearest to spot
    flip = None
    cum = 0.0
    prev = None
    for s in strikes:
        prev, cum = cum, cum + net[s]
        if prev is not None and prev * cum < 0:
            flip = s
            break
    if flip is None and strikes and spot:
        flip = min(strikes, key=lambda s: abs(s - spot))

    # gamma walls: top 3 strikes by |net GEX|
    walls = sorted(strikes, key=lambda s: abs(net[s]), reverse=True)[:3]
    walls = [{"strike": w, "net_gex_bn": round(net[w] / 1e9, 3)} for w in walls]

    # max pain: strike minimizing total intrinsic payout of all OI
    max_pain = None
    if strikes:
        best, best_val = None, None
        for s in strikes:
            payout = 0.0
            for ks, o in oi_call.items():
                payout += max(s - ks, 0) * o
            for ks, o in oi_put.items():
                payout += max(ks - s, 0) * o
            if best_val is None or payout < best_val:
                best, best_val = s, payout
        max_pain = best

    return {
        "spot": spot,
        "chain_timestamp": ts,
        "contracts_analyzed": len(per_strike),
        "net_gex_bn": round((total_call - total_put), 3),
        "call_gex_bn": round(total_call, 3),
        "put_gex_bn": round(total_put, 3),
        "flip_estimate": flip,
        "gamma_walls": walls,
        "max_pain": max_pain,
    }


def write(snapshot):
    with open(os.path.join(OUTDIR, "cboe-gex-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "cboe-gex-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "CBOE delayed quotes (cdn.cboe.com, keyless)",
        "status": "ok",
        "symbols": {},
    }
    try:
        for sym in SYMBOLS:
            payload = fetch_chain(sym)
            snapshot["symbols"][sym] = analyze(payload)
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: cboe-gex feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    for sym, a in snapshot["symbols"].items():
        spot = a.get("spot")
        flip = a.get("flip_estimate")
        if spot and flip:
            dist = (spot - flip) / spot
            tag = "ABOVE flip (dealers dampen)" if dist > 0 else "BELOW flip (dealers amplify)"
            if abs(dist) < 0.01:
                print(f"ALERT cboe-gex {sym}: spot {spot:.0f} within 1% of flip {flip:.0f} — {tag}")
            else:
                print(f"OK cboe-gex {sym}: spot {spot:.0f}, flip ~{flip:.0f} ({tag}), "
                      f"net GEX ${a['net_gex_bn']:.1f}bn, max pain {a['max_pain']}")
        else:
            print(f"OK cboe-gex {sym}: incomplete data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
