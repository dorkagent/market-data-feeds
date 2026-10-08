#!/usr/bin/env python3
"""SEC fails-to-deliver (FTD) feed for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Fails-to-deliver
shares per security, published twice monthly (a/b halves), ~2-week lag.
Settlement-stress / squeeze-risk gauge; pairs with FINRA short volume (flow)
and FINRA short interest (positions).

Source (free, no key): https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data
  Files: https://www.sec.gov/files/data/fails-deliver-data/cnsfailsYYYYMM[a|b].zip
  Pipe-delimited TXT: SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
  SEC fair-access: descriptive User-Agent required.

Idempotent: the script processes only a NEW half-month file; if the latest
available file matches the last-processed one (state file), it writes a
"no_new_data" snapshot and exits 0.

Writes:
  hidden_files/sec-ftd/sec-ftd-latest.json
  hidden_files/sec-ftd/sec-ftd-history.jsonl
  hidden_files/sec-ftd/state.json  (last processed file)

Stdout: ALERT lines for notable FTD levels; OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import io
import json
import os
import re
import sys
import zipfile

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "sec-ftd")
STATE = os.path.join(OUTDIR, "state.json")

UA = "DorkPaperTrading/1.0 (research; contact dork@agentmail.to)"
INDEX = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
FILEURL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{name}.zip"
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
ALERT_QTY = 100_000  # fails share level worth flagging as context

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Biweekly FTD levels; ~2-week lag; settlement-stress gauge.")


def latest_file():
    r = requests.get(INDEX, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    names = sorted(set(re.findall(r"cnsfails(\d{6}[ab])", r.text)))
    if not names:
        raise RuntimeError("no cnsfails files found on index page")
    return names[-1]


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def fetch_ftd(name):
    r = requests.get(FILEURL.format(name=name), headers={"User-Agent": UA},
                     timeout=120)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    txt = zf.namelist()[0]
    rows = {}
    with zf.open(txt) as f:
        for line in io.TextIOWrapper(f, errors="replace"):
            parts = line.rstrip("\n").split("|")
            if len(parts) < 6:
                continue
            sym = parts[2].strip()
            if sym in WATCHLIST:
                try:
                    qty = int(parts[3].replace(",", ""))
                except ValueError:
                    continue
                rows[sym] = {"settlement_date": parts[0].strip(),
                             "fails_qty": qty,
                             "price": parts[5].strip()}
    return rows


def write(snapshot):
    with open(os.path.join(OUTDIR, "sec-ftd-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "sec-ftd-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "SEC fails-to-deliver (sec.gov, keyless ZIP)",
        "status": "ok",
        "tickers": {},
    }
    try:
        name = latest_file()
        snapshot["file"] = name
        state = load_state()
        if state.get("last_file") == name and state.get("tickers"):
            snapshot["status"] = "no_new_data"
            snapshot["tickers"] = state["tickers"]
            snapshot["note"] += f" Latest available file {name} already processed."
            write(snapshot)
            print(f"OK sec-ftd: no new half-month file (latest {name} already processed)")
            return 0
        rows = fetch_ftd(name)
        prev = state.get("tickers", {})
        for sym in WATCHLIST:
            cur = rows.get(sym)
            if not cur:
                snapshot["tickers"][sym] = {"error": "not in file"}
                continue
            entry = dict(cur)
            p = prev.get(sym, {})
            if p.get("fails_qty") is not None:
                entry["delta_vs_prior_half"] = cur["fails_qty"] - p["fails_qty"]
                entry["prior_half_file"] = state.get("last_file")
            snapshot["tickers"][sym] = entry
        save_state({"last_file": name, "tickers": snapshot["tickers"]})
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: sec-ftd feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    flagged = [(s, e) for s, e in snapshot["tickers"].items()
               if e.get("fails_qty", 0) >= ALERT_QTY]
    for sym, e in flagged:
        d = e.get("delta_vs_prior_half")
        ds = f" (Δ {d:+,} vs prior half)" if d is not None else ""
        print(f"ALERT sec-ftd {sym}: {e['fails_qty']:,} fails on {e['settlement_date']}{ds}")
    if not flagged:
        lvls = {s: e.get("fails_qty") for s, e in snapshot["tickers"].items()}
        print(f"OK sec-ftd: file {name}; fails levels={lvls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
