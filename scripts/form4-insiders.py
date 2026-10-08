#!/usr/bin/env python3
"""
form4-insiders.py — SEC EDGAR Form 4 insider-transaction feed.

Universe: AAPL, MSFT, NVDA (SPY/QQQ are ETFs — no Form 4 insider filings exist).
Pipeline (per ticker, stdlib + requests only):
  1. data.sec.gov/submissions/CIK<10-digit>.json -> recent Form 4 accession numbers
  2. www.sec.gov Archives index.json per accession -> find the Form 4 XML doc
  3. Parse nonDerivativeTable: keep only open-market BUYS
     (transactionAcquiringDisposingCode == "P" and acquired/disposed == "A").
     A = grant/award, M = option exercise/conversion -> NOT counted as buys.
     D = disposition to issuer, S = open-market sale -> recorded for context only.

Flags (per ticker, rolling 30 days):
  - >= 3 distinct insiders with open-market buys (P) in the window, OR
  - any single P transaction > $500k.

Outputs:
  hidden_files/form4/form4-latest.json   (per-ticker aggregates + flags)
  hidden_files/form4/form4-history.jsonl (one JSON snapshot line per run)

SEC courtesy: declared User-Agent, ~0.3s delay between requests, well under
the ~10 req/s SEC limit.

Context layer — informs, never triggers trades alone.
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
import xml.etree.ElementTree as ET

UA = "Dork dork@agentmail.to"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "hidden_files", "form4")
LATEST = os.path.join(OUT_DIR, "form4-latest.json")
HISTORY = os.path.join(OUT_DIR, "form4-history.jsonl")

CIKS = {
    "AAPL": "0000320193",   # Apple Inc.
    "MSFT": "0000789019",   # Microsoft Corp
    "NVDA": "0001045810",   # NVIDIA Corp
    "SPY": None,            # ETF — no Form 4 insider filings
    "QQQ": None,            # ETF — no Form 4 insider filings
}

LOOKBACK_FILINGS_DAYS = 90   # scan filings filed within this window
WINDOW_DAYS = 30             # flag window for P transactions
MAX_FILINGS_PER_TICKER = 12  # request cap per ticker
DELAY = 0.35
LARGE_BUY = 500_000


def _request(method, url, **kwargs):
    """GET with retry on transient network failures (egress proxy flakiness)."""
    last = None
    for attempt in range(4):
        try:
            time.sleep(DELAY)
            r = SESSION.request(method, url, timeout=60, **kwargs)
            r.raise_for_status()
            return r
        except requests.HTTPError:
            raise  # 4xx/5xx = real response, don't retry blindly
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def get_json(url):
    return _request("get", url).json()


def get_text(url):
    return _request("get", url).text


def local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(node):
    """First non-empty text of node or any descendant (Form 4 values nest in <value>)."""
    if node is None:
        return None
    if node.text and node.text.strip():
        return node.text.strip()
    for d in node.iter():
        if d is not node and d.text and d.text.strip():
            return d.text.strip()
    return None


def _find(node, *names):
    for sub in node.iter():
        if local(sub.tag) in names:
            return sub
    return None


def parse_form4_xml(xml_text):
    """Return list of transactions from a Form 4 ownershipDocument."""
    root = ET.fromstring(xml_text)
    if local(root.tag) != "ownershipDocument":
        return []

    owners = []
    for ro in root.iter():
        if local(ro.tag) == "reportingOwner":
            name = _text(_find(ro, "rptOwnerName"))
            cik = _text(_find(ro, "rptOwnerCik"))
            owners.append((name, cik))
    owner = owners[0] if owners else (None, None)

    txs = []
    for node in root.iter():
        tag = local(node.tag)
        if tag not in ("nonDerivativeTransaction", "derivativeTransaction"):
            continue
        coding = _find(node, "transactionCoding")
        code = _text(_find(coding, "transactionCode")) if coding is not None else None
        acq_disp = _text(_find(node, "transactionAcquiredDisposedCode",
                              "acquiredDisposedCode"))
        shares = None
        price = None
        try:
            if _text(_find(node, "transactionShares")):
                shares = float(_text(_find(node, "transactionShares")).replace(",", ""))
        except ValueError:
            pass
        try:
            if _text(_find(node, "transactionPricePerShare")):
                price = float(_text(_find(node, "transactionPricePerShare")).replace(",", ""))
        except ValueError:
            pass
        txdate = _text(_find(node, "transactionDate"))
        if txdate:
            txdate = txdate[:10]
        txs.append({
            "table": "derivative" if tag == "derivativeTransaction" else "non-derivative",
            "owner": owner[0], "owner_cik": owner[1],
            "code": code, "acquired_disposed": acq_disp,
            "shares": shares, "price": price,
            "value": (shares * price) if (shares and price) else None,
            "date": txdate,
        })
    return txs


def fetch_ticker(ticker, cik, errors):
    """Return dict with buys/sales lists for a ticker."""
    subs = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])

    cutoff = date.today() - timedelta(days=LOOKBACK_FILINGS_DAYS)
    targets = []
    for form, acc, fdate in zip(forms, accs, filing_dates):
        if form != "4":  # skip amendments (4/A) and everything else
            continue
        try:
            fd = datetime.strptime(fdate, "%Y-%m-%d").date()
        except ValueError:
            continue
        if fd >= cutoff:
            targets.append((fd, acc))
    targets.sort(reverse=True)
    targets = targets[:MAX_FILINGS_PER_TICKER]

    cik_int = str(int(cik))
    buys, sales = [], []
    scanned = 0
    for fdate, acc in targets:
        try:
            acc_nodash = acc.replace("-", "")
            idx = get_json(
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
            )
            items = idx.get("directory", {}).get("item", [])
            xml_name = None
            for it in items:
                name = it.get("name", "")
                if name.endswith(".xml") and "index" not in name.lower():
                    # prefer the primary Form 4 doc; first non-index .xml usually is it
                    if xml_name is None or "form4" in name.lower() or "primary" in name.lower():
                        xml_name = name
            if not xml_name:
                errors.append(f"{ticker} {acc}: no Form 4 XML in index")
                continue
            xml_text = get_text(
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{xml_name}"
            )
            txs = parse_form4_xml(xml_text)
            scanned += 1
            for t in txs:
                t["ticker"] = ticker
                t["filing_date"] = fdate.isoformat()
                t["accession"] = acc
                if t["code"] == "P" and t["acquired_disposed"] == "A":
                    buys.append(t)
                elif t["code"] in ("S", "D") and t["acquired_disposed"] == "D":
                    sales.append(t)
        except Exception as e:  # noqa: BLE001 — per-filing resilience
            errors.append(f"{ticker} {acc}: {type(e).__name__}: {e}")
    return {"filings_scanned": scanned, "filings_targeted": len(targets),
            "buys": buys, "sales": sales}


def _in_window(rec, cutoff):
    try:
        return datetime.strptime((rec.get("date") or "")[:10], "%Y-%m-%d").date() >= cutoff
    except ValueError:
        return False


def summarize(ticker, data):
    cutoff = date.today() - timedelta(days=WINDOW_DAYS)
    buys = [b for b in data["buys"] if _in_window(b, cutoff)]
    sales = [s for s in data["sales"] if _in_window(s, cutoff)]
    insiders = {b["owner_cik"] or b["owner"] for b in buys if b.get("owner")}
    total = sum(b["value"] for b in buys if b.get("value"))
    mx = max((b["value"] for b in buys if b.get("value")), default=0)

    flags = []
    if len(insiders) >= 3:
        flags.append({
            "type": "cluster",
            "detail": f"{len(insiders)} distinct insiders with open-market buys (P) in {WINDOW_DAYS}d",
        })
    big = [b for b in buys if b.get("value") and b["value"] > LARGE_BUY]
    for b in big:
        flags.append({
            "type": "large_buy",
            "detail": (f"{b.get('owner') or 'unknown'} bought "
                       f"${b['value']:,.0f} ({b.get('shares') or '?'} sh @ "
                       f"${b['price']:.2f}) on {b.get('date')}"),
        })
    return {
        "filings_scanned": data["filings_scanned"],
        "filings_targeted": data["filings_targeted"],
        "p_buys_30d": buys,
        "distinct_insiders_30d": len(insiders),
        "total_value_30d": round(total, 2),
        "max_single_30d": round(mx, 2),
        "sales_30d_count": len(sales),
        "flags": flags,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    errors = []
    tickers = {}
    all_flags = []
    for ticker, cik in CIKS.items():
        if cik is None:
            tickers[ticker] = {
                "status": "n/a",
                "note": "ETF — no Form 4 insider filings exist",
                "flags": [],
            }
            continue
        try:
            data = fetch_ticker(ticker, cik, errors)
            summary = summarize(ticker, data)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ticker}: FETCH FAILED — {type(e).__name__}: {e}")
            summary = {"status": "error", "flags": []}
        tickers[ticker] = summary
        for f in summary.get("flags", []):
            all_flags.append({"ticker": ticker, **f})

    latest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": list(CIKS),
        "note": "Context layer — informs, never triggers trades alone.",
        "tickers": tickers,
        "flags": all_flags,
        "errors": errors,
    }
    with open(LATEST, "w") as f:
        json.dump(latest, f, indent=2)

    snapshot = {
        "date": date.today().isoformat(),
        "generated_at": latest["generated_at"],
        "flags": all_flags,
        "errors": errors,
        "per_ticker": {
            t: {
                "filings_scanned": s.get("filings_scanned"),
                "distinct_insiders_30d": s.get("distinct_insiders_30d"),
                "total_value_30d": s.get("total_value_30d"),
                "max_single_30d": s.get("max_single_30d"),
                "flag_types": [f["type"] for f in s.get("flags", [])],
            } for t, s in tickers.items()
        },
    }
    with open(HISTORY, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    print(f"wrote {LATEST}")
    print(f"flags: {len(all_flags)}")
    for fl in all_flags:
        print(f"  FLAG {fl['ticker']} [{fl['type']}] {fl['detail']}")
    if errors:
        print(f"errors: {len(errors)}")
        for e in errors[:10]:
            print(f"  ERR {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
