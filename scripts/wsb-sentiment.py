#!/usr/bin/env python3
"""
Retail-sentiment snapshot for the Alpaca paper-trading monitors.

Fetches recent posts from configured subreddits via the Arctic Shift
archive API (direct reddit.com is blocked from this VM), scores ticker
mentions with a bull/bear keyword lean, and writes JSON snapshots that
the signal-scan and minute-monitor crons read for retail-sentiment
context.

Usage:
    wsb-sentiment.py [--subs wallstreetbets] [--pages 5]

Output (under hidden_files/sentiment/):
    <sub>-latest.json   full snapshot the monitors read
    <sub>-history.jsonl one compact line per run (for mention velocity)

Requires: python3, stdlib only.
"""
import argparse, collections, datetime, json, os, re, sys, time, urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "hidden_files", "sentiment")

BULLW = re.compile(
    r"\b(calls?|moon|bullish|buying|bought|long|tendies|rocket|to the moon|"
    r"undervalued|breakout|squeeze|melt up|rip)\b", re.I)
BEARW = re.compile(
    r"\b(puts?|bearish|shorting|short|selling|sold|dump|crash|baghold|"
    r"rug|overvalued|melt down|puts printing)\b", re.I)
# Bare (no $) matches only against this known list to avoid noise.
KNOWN = set(
    "NVDA TSLA AMD AAPL MSFT SPY QQQ META AMZN NFLX PLTR COIN MSTR GME AMC MU "
    "AVGO NBIS CRWV OPEN SOFI HOOD DJT SMCI ARM MRVL LRCX INTC ORCL APP RDDT "
    "DKNG AFRM UPST RKLB ASTS IONQ RIGETTI QBTS DOGE SHIB PEPE BTC ETH SOL XRP "
    "TRUMP BONK WIF TLT UBER SEZL MOD QNT".split())
STOP = {"DD", "YOLO", "WSB", "CEO", "IPO", "ETF", "THE", "AND", "FOR", "YOU",
        "ARE", "NOT", "BUT", "ALL", "HAS", "HAVE", "THIS", "THAT", "WITH",
        "FROM", "WHAT", "WHEN", "WILL", "JUST", "LIKE", "GET", "GOT", "NOW",
        "NEW", "OUT", "SEE", "BIG", "DAY", "WAY", "ONE", "TWO", "USA", "GDP",
        "CPI", "FED", "POTUS"}


def fetch(url):
    """GET JSON with retries (VM egress drops ~30% of HTTPS requests)."""
    for i in range(6):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
                if data.get("data"):
                    return data["data"]
                return data.get("data") or []
        except Exception:
            time.sleep(3)
    print(f"WARN: failed to fetch {url[:80]}", file=sys.stderr)
    return []


def get_posts(sub, pages):
    posts, before = [], None
    for _ in range(pages):
        url = (f"https://arctic-shift.photon-reddit.com/api/posts/search"
               f"?subreddit={sub}&sort=desc&limit=100")
        if before:
            url += f"&before={before}"
        batch = fetch(url)
        if not batch:
            break
        posts.extend(batch)
        before = min(p["created_utc"] for p in batch) - 1
        if len(batch) < 100:
            break
    return posts


def analyze(sub, posts):
    tickers = collections.Counter()
    bull = collections.Counter()
    bear = collections.Counter()
    eng = collections.Counter()
    for p in posts:
        title = p.get("title") or ""
        text = title + " " + (p.get("selftext") or "")[:2000]
        found = {m for m in re.findall(r"\$([A-Z]{2,5})\b", text)
                 if m not in STOP}
        bare = {m for m in re.findall(r"\b([A-Z]{3,5})\b", title)
                if m in KNOWN}
        w = (p.get("score") or 0) + (p.get("num_comments") or 0)
        for t in found | bare:
            tickers[t] += 1
            eng[t] += w
            if BULLW.search(text):
                bull[t] += 1
            if BEARW.search(text):
                bear[t] += 1
    top_tickers = [
        {"ticker": t, "mentions": n, "bull": bull[t], "bear": bear[t],
         "engagement": eng[t]}
        for t, n in tickers.most_common(20)]
    top_posts = [
        {"title": (p.get("title") or "")[:140],
         "score": p.get("score") or 0,
         "num_comments": p.get("num_comments") or 0,
         "created_utc": p.get("created_utc"),
         "url": f"https://www.reddit.com/r/{sub}/comments/{p.get('id')}/"}
        for p in sorted(posts, key=lambda x: x.get("score", 0),
                        reverse=True)[:15]]
    now = datetime.datetime.now(datetime.timezone.utc).astimezone()
    oldest = min((p["created_utc"] for p in posts), default=None)
    return {
        "subreddit": sub,
        "generated_at": now.isoformat(),
        "posts_analyzed": len(posts),
        "window_start": (datetime.datetime.fromtimestamp(
            oldest, datetime.timezone.utc).astimezone().isoformat()
            if oldest else None),
        "top_tickers": top_tickers,
        "top_posts": top_posts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs", default="wallstreetbets")
    ap.add_argument("--pages", type=int, default=5)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    for sub in [s.strip() for s in args.subs.split(",") if s.strip()]:
        posts = get_posts(sub, args.pages)
        if not posts:
            print(f"{sub}: no posts fetched, skipping", file=sys.stderr)
            continue
        snap = analyze(sub, posts)
        with open(os.path.join(OUT_DIR, f"{sub}-latest.json"), "w") as f:
            json.dump(snap, f, indent=1)
        hist = {"generated_at": snap["generated_at"],
                "posts": snap["posts_analyzed"],
                "tickers": {t["ticker"]: [t["mentions"], t["bull"], t["bear"]]
                            for t in snap["top_tickers"][:10]}}
        with open(os.path.join(OUT_DIR, f"{sub}-history.jsonl"), "a") as f:
            f.write(json.dumps(hist) + "\n")
        print(f"{sub}: {len(posts)} posts, "
              f"{len(snap['top_tickers'])} tickers -> {sub}-latest.json")


if __name__ == "__main__":
    main()
