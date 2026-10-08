#!/usr/bin/env python3
"""bin/edgar.py -- SEC EDGAR official-API CLI for the Alpaca paper-trading simulator.

Subcommands:
  cik       --ticker NVDA                    resolve ticker -> 10-digit CIK
  recent-8k --ticker NVDA [--days 7]         8-K event detection for one ticker
  events    --tickers NVDA,AAPL [--days 7]   8-K event sweep across tickers
  facts     --ticker NVDA [--concept Revenues]  XBRL companyfacts fundamentals

DATA FEED ONLY -- context layer, never a trade trigger. Informs, never fires
trades alone. Output is JSON to stdout; snapshots land under
hidden_files/edgar/ (edgar-latest.json). Every API call is logged to
hidden_files/edgar/edgar-api-calls.log with source=edgar.py.

Official endpoints (free, no key, no signup):
  https://www.sec.gov/files/company_tickers.json          ticker -> CIK
  https://efts.sec.gov/LATEST/search-index                full-text search
  https://data.sec.gov/api/xbrl/companyfacts/CIK....json  XBRL fundamentals

SEC fair-access rules: a descriptive User-Agent on EVERY request (403 without
it) and max 10 requests/second. This script sleeps 0.2s between calls (~5/sec)
and caches company_tickers.json for 24h.

Event classification for 8-Ks (by Item number + keyword hits):
  earnings    2.02 (results of operations), 9.01 exhibit = earnings release
  mna         1.01 (material agreement), 2.01 (acquisition completion)
  leadership  5.02 (departure/election of officers/directors)
  guidance    keyword hit: "guidance" / "outlook" / "forecast" in filing
  other       8.01 (other events) and everything else -- news-pass material
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GOAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(GOAL_DIR, "hidden_files", "edgar")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
API_CALL_LOG = os.path.join(OUT_DIR, "edgar-api-calls.log")

UA = "DorkPaperTrading/1.0 (dork@agentmail.to)"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

NOTE = ("Data feed only — context layer, never a trade trigger. "
        "8-K/Form-4 event trigger informs the Monday rotation and the "
        "premarket brief; it never fires a trade alone.")

ITEM_EVENTS = {
    "2.02": "earnings",      # Results of operations and financial condition
    "2.01": "mna",           # Completion of acquisition or disposition
    "1.01": "mna",           # Entry into material definitive agreement
    "1.02": "mna",           # Termination of material definitive agreement
    "5.02": "leadership",    # Departure/election of directors or officers
    "8.01": "other",         # Other events (often news-worthy)
}


def api_get(url, params=None, source="edgar.py", retries=1):
    """GET with SEC-required UA; logs every call; honors 10 req/sec.
    Retries once on 5xx — the FTS index occasionally 500s under load."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_err = None
    body = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            last_err = None
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (500, 502, 503, 504) and attempt < retries:
                print(f"[API] {e.code} on {url[:120]} — retrying once",
                      file=sys.stderr)
                time.sleep(2)
                continue
            raise
    if body is None:
        raise last_err
    # API-call log: every call, with source (task requirement)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    line = f"{ts} source={source} GET {url[:220]} status=200 bytes={len(body)}"
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(API_CALL_LOG, "a") as f:
        f.write(line + "\n")
    print(f"[API] {line}", file=sys.stderr)
    time.sleep(0.2)  # ~5 req/sec, inside the 10/sec fair-access limit
    return json.loads(body)


def load_tickers(force_refresh=False):
    """ticker -> (cik10, title); cached 24h."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, "company_tickers.json")
    fresh = (os.path.exists(cache) and
             time.time() - os.path.getmtime(cache) < 24 * 3600)
    if not (fresh or force_refresh):
        try:
            data = api_get(TICKERS_URL)
            with open(cache, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"[API] ticker list refresh failed ({e}); using cache",
                  file=sys.stderr)
            data = json.load(open(cache)) if os.path.exists(cache) else {}
    else:
        data = json.load(open(cache))
    return {v["ticker"]: (str(v["cik_str"]).zfill(10), v["title"])
            for v in data.values()}


def resolve_cik(ticker, tickers):
    t = ticker.strip().upper()
    if t not in tickers:
        raise ValueError(f"ticker {t} not found in SEC company_tickers.json")
    return tickers[t]


def search_filings(cik, forms, startdt, enddt, q="", size=100):
    params = {
        "dateRange": "custom",
        "startdt": startdt,
        "enddt": enddt,
        "forms": forms,
        "ciks": cik,
        "size": size,
    }
    if q:
        params["q"] = q
    return api_get(FTS_URL, params=params)


def classify_8k(hit):
    """Return (event_tags, filing_dict) for one FTS 8-K hit."""
    src = hit.get("_source", {})
    items = [i.strip() for i in (src.get("items") or [])]
    tags = sorted({ITEM_EVENTS.get(i, "other") for i in items})
    company = (src.get("display_names") or ["?"])[0]
    return tags, {
        "company": company,
        "form": src.get("form"),
        "file_date": src.get("file_date"),
        "items": items,
        "event_tags": tags,
        "ciks": src.get("ciks"),
    }


def cmd_cik(args):
    cik, title = resolve_cik(args.ticker, load_tickers())
    print(json.dumps({"ticker": args.ticker.upper(), "cik": cik,
                      "title": title, "note": NOTE}, indent=2))


def cmd_recent_8k(args):
    tickers = load_tickers()
    cik, title = resolve_cik(args.ticker, tickers)
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    startdt, enddt = start.isoformat(), end.isoformat()

    data = search_filings(cik, "8-K", startdt, enddt)
    hits = data.get("hits", {}).get("hits", [])
    filings = []
    for h in hits:
        tags, f = classify_8k(h)
        f["ticker"] = args.ticker.upper()
        filings.append(f)
    filings.sort(key=lambda f: f["file_date"], reverse=True)

    # guidance-language pass over 8-K/10-Q/10-K for the same ticker
    gdata = search_filings(
        cik, "8-K,10-Q,10-K", startdt, enddt,
        q='"guidance" OR "outlook" OR "forecast"', size=50)
    guidance = []
    for h in gdata.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        guidance.append({
            "form": src.get("form"),
            "file_date": src.get("file_date"),
            "items": src.get("items"),
            "company": (src.get("display_names") or ["?"])[0],
        })

    out = {
        "ticker": args.ticker.upper(),
        "cik": cik,
        "title": title,
        "window": {"startdt": startdt, "enddt": enddt},
        "filings_8k": filings,
        "filings_8k_count": len(filings),
        "event_summary": {ev: sum(1 for f in filings if ev in f["event_tags"])
                          for ev in ("earnings", "mna", "leadership",
                                     "guidance", "other")},
        "guidance_language_hits": guidance,
        "source": "SEC EDGAR full-text search (efts.sec.gov, keyless)",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
    }
    print(json.dumps(out, indent=2))
    return out


def cmd_events(args):
    tickers = load_tickers()
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    startdt, enddt = start.isoformat(), end.isoformat()
    results = []
    for t in [x.strip().upper() for x in args.tickers.split(",") if x.strip()]:
        try:
            cik, title = resolve_cik(t, tickers)
        except ValueError as e:
            results.append({"ticker": t, "status": "error",
                            "error": str(e)})
            continue
        try:
            data = search_filings(cik, "8-K", startdt, enddt)
        except Exception as e:
            # transient SEC hiccup on one name must not kill the sweep
            print(f"[API] {t}: FTS search failed ({e}) — recorded, continuing",
                  file=sys.stderr)
            results.append({"ticker": t, "cik": cik, "title": title,
                            "status": "error",
                            "error": f"FTS search failed: {e}"})
            continue
        filings = []
        for h in data.get("hits", {}).get("hits", []):
            _, f = classify_8k(h)
            f["ticker"] = t
            filings.append(f)
        filings.sort(key=lambda f: f["file_date"], reverse=True)
        notable = [f for f in filings
                   if any(e in f["event_tags"]
                          for e in ("earnings", "mna", "leadership"))]
        results.append({
            "ticker": t, "cik": cik, "title": title,
            "filings_8k": filings, "filings_8k_count": len(filings),
            "notable": notable,
        })

    out = {
        "tickers": [x.strip().upper() for x in args.tickers.split(",")
                    if x.strip()],
        "window": {"startdt": startdt, "enddt": enddt},
        "days": args.days,
        "companies": results,
        "flagged": [r for r in results if r.get("notable")],
        "source": "SEC EDGAR full-text search (efts.sec.gov, keyless)",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
    }
    with open(os.path.join(OUT_DIR, "edgar-latest.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    return out


def cmd_facts(args):
    tickers = load_tickers()
    cik, title = resolve_cik(args.ticker, tickers)
    data = api_get(FACTS_URL.format(cik=cik))
    facts = data.get("facts", {})
    if args.concept:
        concepts = [args.concept]
    else:
        concepts = ["Revenues", "NetIncomeLoss", "EarningsPerShareDiluted",
                    "Assets", "StockholdersEquity",
                    "CashAndCashEquivalentsAtCarryingValue"]
    out = {"ticker": args.ticker.upper(), "cik": cik,
           "entityName": data.get("entityName", title), "concepts": {}}
    for concept in concepts:
        entry = None
        for tax in ("us-gaap", "dei"):
            entry = facts.get(tax, {}).get(concept)
            if entry:
                break
        if not entry:
            out["concepts"][concept] = {"status": "not_found"}
            continue
        points = []
        for unit, vals in entry.get("units", {}).items():
            for v in vals:
                points.append({"fy": v.get("fy"), "fp": v.get("fp"),
                               "end": v.get("end"), "val": v.get("val"),
                               "unit": unit, "form": v.get("form")})
        # annual (10-K) points first, then most recent 4
        annual = [p for p in points if p["form"] == "10-K"]
        latest = sorted(points, key=lambda p: p["end"] or "",
                        reverse=True)[:4]
        out["concepts"][concept] = {
            "label": entry.get("label"), "description": entry.get("description"),
            "latest_annual": annual[-1] if annual else None,
            "latest_points": latest,
        }
    out["source"] = "SEC EDGAR XBRL companyfacts (data.sec.gov, keyless)"
    out["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out["note"] = NOTE
    print(json.dumps(out, indent=2))
    return out


def main():
    p = argparse.ArgumentParser(
        description="SEC EDGAR official-API CLI (keyless; data feed only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cik", help="resolve ticker -> CIK")
    c.add_argument("--ticker", required=True)
    c.set_defaults(func=cmd_cik)

    r = sub.add_parser("recent-8k", help="8-K event detection for one ticker")
    r.add_argument("--ticker", required=True)
    r.add_argument("--days", type=int, default=7)
    r.set_defaults(func=cmd_recent_8k)

    e = sub.add_parser("events", help="8-K event sweep across tickers")
    e.add_argument("--tickers", required=True,
                   help="comma-separated, e.g. NVDA,AAPL,MSFT")
    e.add_argument("--days", type=int, default=7)
    e.set_defaults(func=cmd_events)

    f = sub.add_parser("facts", help="XBRL companyfacts fundamentals")
    f.add_argument("--ticker", required=True)
    f.add_argument("--concept", help="XBRL concept, e.g. Revenues")
    f.set_defaults(func=cmd_facts)

    args = p.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: edgar.py {args.cmd} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
