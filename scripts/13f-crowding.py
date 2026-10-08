#!/usr/bin/env python3
"""Quarterly 13F crowding check for the Alpaca paper-trading simulator.

RISK-CONTEXT LAYER ONLY — never a trade trigger. 13F-HR filings lag up to
~75 days behind quarter-end, so this measures crowded-long fragility
(how many whales are piled into our names), not entries.

Data: SEC EDGAR (no key). Per whale:
  1. https://data.sec.gov/submissions/CIK##########.json -> latest 13F-HR / 13F-HR/A
  2. filing index.json -> discover the informationTable XML
  3. parse holdings, match our universe by CUSIP

Per ticker: whales holding it, aggregate reported value, crowding rating
(crowded >= 8 of 12 whales, moderate 4-7, sparse 0-3).

Writes:
  hidden_files/13f/crowding-latest.json   (read by the monitors)
  hidden_files/13f/crowding-YYYY-QN.json   (dated snapshot)
  hidden_files/13f/changes-YYYY-QN.md      (diff vs prior quarter)

Prints ALERT lines only when a current holding flips to crowded
(fragility warning) or gets widely abandoned. The cron stays quiet
otherwise.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

UA = "Dork dork@agentmail.to"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "13f")
WATCHLIST = os.path.join(BASE, "strategy", "watchlist.md")

# All CIKs verified against EDGAR 2026-10-01. Never guess a CIK.
WHALES = [
    ("Berkshire Hathaway Inc", "0001067983"),
    ("Citadel Advisors LLC", "0001423053"),
    ("Millennium Management LLC", "0001273087"),
    ("Bridgewater Associates LP", "0001350694"),
    ("Tiger Global Management LLC", "0001167483"),
    ("Pershing Square Capital Mgmt LP", "0001336528"),
    ("Renaissance Technologies LLC", "0001037389"),
    ("Viking Global Investors LP", "0001103804"),
    ("Coatue Management LLC", "0001135730"),
    ("Lone Pine Capital LLC", "0001061165"),
    ("Appaloosa LP", "0001656456"),
    ("Two Sigma Advisers LP", "0001478735"),
]

# CUSIPs verified against live EDGAR infoTables 2026-10-01
# (SPY/QQQ/NVDA/MSFT via Citadel 13F-HR; AAPL via Berkshire 13F-HR).
UNIVERSE = {
    "SPY":  ("78462F103", "SPDR"),
    "QQQ":  ("46090E103", "QQQ"),
    "NVDA": ("67066G104", "NVIDIA"),
    "AAPL": ("037833100", "APPLE"),
    "MSFT": ("594918104", "MICROSOFT"),
}
CUSIP_TO_TICKER = {c: t for t, (c, _) in UNIVERSE.items()}

CROWDED_MIN, MODERATE_MIN = 8, 4  # of 12 whales


def fetch(url, timeout=90, tries=6):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # transient VM egress flakiness
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed after {tries} tries: {url}: {last}")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def to_usd(value_raw, shares):
    """Normalize a 13F 'value' field to USD.

    The spec says thousands, but several large filers submit whole dollars.
    Self-calibrating: value_raw / shares should approximate the stock's
    quarter-end price. In dollars the ratio lands near the share price
    ($100s); in thousands it lands near price/1000 (< $1). Our universe
    trades $100-$800, so 50 separates cleanly.
    """
    if shares and value_raw / shares > 50:
        return value_raw  # filer reported whole dollars
    return value_raw * 1000  # spec: thousands


def find_text(elem, *names):
    for child in elem:
        if local(child.tag) in names:
            return (child.text or "").strip()
    return ""


def latest_13f(cik):
    """Return (accession, filing_date, report_date, nt_newer) for the latest
    13F-HR(/A). If the manager's newest 13F-form filing is a 13F-NT (holdings
    reported elsewhere / nothing reportable that quarter), we still use their
    most recent 13F-HR and set nt_newer=True so the snapshot says so."""
    d = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    recent = d["filings"]["recent"]
    best_hr, newest_nt = None, None
    n = len(recent["form"])
    for i in range(n):
        form = recent["form"][i]
        if form not in ("13F-HR", "13F-HR/A", "13F-NT"):
            continue
        fdate = recent["filingDate"][i]
        rdate = recent["reportDate"][i] if "reportDate" in recent else ""
        entry = (recent["accessionNumber"][i], fdate, rdate)
        if form == "13F-NT":
            if newest_nt is None or fdate > newest_nt[1]:
                newest_nt = entry
        elif best_hr is None or fdate > best_hr[1]:
            best_hr = entry
    if not best_hr:
        return None
    acc, fdate, rdate = best_hr
    nt_newer = bool(newest_nt and newest_nt[1] > fdate)
    return acc, fdate, rdate, nt_newer


def infotable_xml(cik_int, accession):
    """Discover and return the parsed informationTable root."""
    nodash = accession.replace("-", "")
    idx = json.loads(fetch(
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/index.json"))
    items = idx["directory"]["item"]
    cands = [it["name"] for it in items
             if it["name"].lower().endswith(".xml")
             and "primary" not in it["name"].lower()]
    for name in cands:
        time.sleep(0.4)
        try:
            root = ET.fromstring(fetch(
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{name}"))
        except Exception:
            continue
        if local(root.tag) == "informationTable":
            return root
    raise RuntimeError(f"no informationTable found in {accession}")


def whale_holdings(name, cik):
    """Return (meta, error, holds) where holds is ticker ->
    {long_value_usd, long_shares, option_value_usd}.

    Only plain long-stock rows (no putCall element) count toward crowding.
    PUT/CALL option rows share the same CUSIP but are hedges/overlays, not
    crowded-long exposure, so their notional is tracked separately."""
    time.sleep(0.4)  # stay well under EDGAR's 10 req/sec
    found = latest_13f(cik)
    if not found:
        return None, "no 13F-HR found", {}
    acc, fdate, rdate, nt_newer = found
    time.sleep(0.4)
    root = infotable_xml(int(cik), acc)
    holds = {}
    for it in root:
        if local(it.tag) != "infoTable":
            continue
        cusip = find_text(it, "cusip").upper()
        if cusip not in CUSIP_TO_TICKER:
            continue
        ticker = CUSIP_TO_TICKER[cusip]
        # sanity: issuer name should match expectation
        issuer = find_text(it, "nameOfIssuer").upper()
        expect = UNIVERSE[ticker][1]
        if expect not in issuer:
            print(f"WARN: {name}: CUSIP {cusip} issuer '{issuer}' "
                  f"does not contain '{expect}' — skipped", file=sys.stderr)
            continue
        try:
            val_raw = int(find_text(it, "value") or 0)
        except ValueError:
            val_raw = 0
        amt = None
        for child in it:  # ElementTree has no local-name() predicate
            if local(child.tag) == "shrsOrPrnAmt":
                amt = child
                break
        shares = 0
        if amt is not None:
            try:
                shares = int(find_text(amt, "sshPrnamt") or 0)
            except ValueError:
                pass
        h = holds.setdefault(ticker, {"long_value_usd": 0, "long_shares": 0,
                                      "option_value_usd": 0})
        val = to_usd(val_raw, shares)
        if find_text(it, "putCall"):  # option overlay, not long exposure
            h["option_value_usd"] += val
        else:
            h["long_value_usd"] += val
            h["long_shares"] += shares
    return (acc, fdate, rdate, nt_newer), None, holds


def current_holdings():
    """Parse the Core section of the watchlist; fallback to SPY/QQQ."""
    try:
        txt = open(WATCHLIST).read()
        core = txt.split("## Core", 1)[1].split("## ", 1)[0]
        syms = re.findall(r"^\|\s*([A-Z]{1,6})\s*\|", core, re.M)
        syms = [s for s in syms if s in UNIVERSE]
        if syms:
            return syms
    except Exception:
        pass
    return ["SPY", "QQQ"]


def crowding(n):
    if n >= CROWDED_MIN:
        return "crowded"
    if n >= MODERATE_MIN:
        return "moderate"
    return "sparse"


def quarter_of(report_date):
    m = int(report_date.split("-")[1])
    q = (m - 1) // 3 + 1
    return f"{report_date[:4]}-Q{q}"


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    holdings_now = current_holdings()

    whales_out, per_ticker, opt_only = [], {t: [] for t in UNIVERSE}, {}
    report_dates = []
    for name, cik in WHALES:
        try:
            meta, err, holds = whale_holdings(name, cik)
        except Exception as e:
            print(f"FAIL {name}: {e}", file=sys.stderr)
            whales_out.append({"name": name, "cik": cik, "status": "failed",
                               "error": str(e)[:200]})
            continue
        if err:
            whales_out.append({"name": name, "cik": cik, "status": "failed",
                               "error": err})
            continue
        acc, fdate, rdate, nt_newer = meta
        if rdate:
            report_dates.append(rdate)
        w = {"name": name, "cik": cik, "status": "ok",
             "accession": acc, "filing_date": fdate, "report_period": rdate}
        if nt_newer:
            w["note"] = ("newer 13F-NT on file — latest 13F-HR used; "
                         "manager reported nothing under own 13F this quarter")
        whales_out.append(w)
        for t, h in holds.items():
            if h["long_value_usd"] > 0:
                per_ticker[t].append({"whale": name,
                                      "long_value_usd": h["long_value_usd"],
                                      "long_shares": h["long_shares"],
                                      "option_value_usd": h["option_value_usd"]})
            elif h["option_value_usd"] > 0:
                opt_only.setdefault(t, []).append(
                    {"whale": name, "option_value_usd": h["option_value_usd"]})

    ok_whales = sum(1 for w in whales_out if w["status"] == "ok")
    quarter = quarter_of(max(set(report_dates), key=report_dates.count)) \
        if report_dates else f"{dt.date.today():%Y}-Q{(dt.date.today().month-1)//3+1}"

    # prior snapshot for diffing
    latest_path = os.path.join(OUTDIR, "crowding-latest.json")
    prior = {}
    if os.path.exists(latest_path):
        try:
            prior = json.load(open(latest_path)).get("tickers", {})
        except Exception:
            prior = {}

    tickers_out, flips = {}, []
    for t in UNIVERSE:
        holders = per_ticker[t]
        n = len(holders)
        agg = sum(h["long_value_usd"] for h in holders)
        opt_agg = (sum(h["option_value_usd"] for h in holders)
                   + sum(h["option_value_usd"] for h in opt_only.get(t, [])))
        rating = crowding(n)
        prev = prior.get(t, {}).get("crowding")
        tickers_out[t] = {
            "cusip": UNIVERSE[t][0],
            "whales_holding_long": n,
            "crowding": rating,
            "prev_crowding": prev,
            "aggregate_long_value_usd": agg,
            "aggregate_option_value_usd": opt_agg,
            "option_only_holders": [h["whale"] for h in opt_only.get(t, [])],
            "holders": sorted(holders, key=lambda h: -h["long_value_usd"]),
        }
        if prev and prev != rating:
            flips.append((t, prev, rating))
        if t in holdings_now and rating == "crowded" and prev != "crowded":
            print(f"ALERT: {t} flipped to CROWDED ({n}/{ok_whales} whales long, "
                  f"${agg:,.0f} aggregate long) — crowded-long fragility on a holding")
        if t in holdings_now and n == 0 and prev not in (None, "sparse"):
            print(f"ALERT: {t} now held long by NO whales (was {prev}) — "
                  f"widely abandoned")

    snapshot = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "quarter": quarter,
        "whales_ok": ok_whales,
        "whales_total": len(WHALES),
        "holdings_checked": holdings_now,
        "whales": whales_out,
        "tickers": tickers_out,
    }
    dated_path = os.path.join(OUTDIR, f"crowding-{quarter}.json")
    json.dump(snapshot, open(dated_path, "w"), indent=1)
    json.dump(snapshot, open(latest_path, "w"), indent=1)

    # changes log
    ch_path = os.path.join(OUTDIR, f"changes-{quarter}.md")
    lines = [f"# 13F crowding changes — {quarter}",
             f"Generated {snapshot['generated_at']} from {ok_whales}/"
             f"{len(WHALES)} whales (report period basis).", ""]
    if not prior:
        lines.append("Baseline snapshot — no prior quarter to diff against.")
    elif not flips:
        lines.append("No crowding-rating flips vs prior quarter.")
    else:
        for t, a, b in flips:
            lines.append(f"- **{t}**: {a} -> **{b}** "
                         f"({tickers_out[t]['whales_holding_long']} whales long)")
    lines += ["", "## Current ratings (long stock only; options excluded)",
              "| Ticker | Whales long | Rating | Agg. long value |",
              "|---|---|---|---|"]
    for t in UNIVERSE:
        o = tickers_out[t]
        lines.append(f"| {t} | {o['whales_holding_long']}/{ok_whales} | "
                     f"{o['crowding']} | ${o['aggregate_long_value_usd']:,.0f} |")
    open(ch_path, "w").write("\n".join(lines) + "\n")

    print(f"wrote {dated_path} ({ok_whales}/{len(WHALES)} whales ok)")
    for t in UNIVERSE:
        o = tickers_out[t]
        print(f"  {t}: {o['crowding']} ({o['whales_holding_long']} whales long, "
              f"${o['aggregate_long_value_usd']:,.0f})")


if __name__ == "__main__":
    main()
