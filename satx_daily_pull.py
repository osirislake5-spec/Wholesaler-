#!/usr/bin/env python3
"""
SATX DAILY WHOLESALE PULL — MAO-first, FSBO/code-case only.

WHAT IT DOES, EVERY RUN:
  1. Pulls Craigslist (by-owner) and FSBO.com San Antonio listings.
  2. Kills mobile homes, lot-rent, agent listings, land, rentals.
  3. Uses the Claude API (Haiku) to read each listing body and extract:
     property type, beds/baths/sqft, stated damage, seller motivation.
  4. Runs MAO = 0.70 * ARV - rehab - fee  (ARV from a zip table you control,
     rehab itemized from the stated damage).
  5. Writes survivors to survivors_YYYY-MM-DD.csv and prints them.
     Anything that fails MAO is written to killed_YYYY-MM-DD.csv with the reason.

WHAT IT DOES NOT DO (be honest with yourself about this):
  - Zillow and Redfin block plain scrapers. They are NOT pulled here.
    Pull those by hand, or add a headless browser later.
  - It cannot get phone numbers. Contact route stays: FSBO.com form,
    Craigslist relay, DSD (210) 207-5422 for code cases, TruePeopleSearch.
  - Zip ARVs below are Zillow zip AVERAGES from 2026-09-20, not comps.
    Update them monthly. A comp from Harold beats any number in this table.

SETUP (once):
  pip install anthropic requests beautifulsoup4
  export ANTHROPIC_API_KEY=sk-ant-...     (your $20 of credits lives here)

RUN (daily, 9am Central — use cron, Replit "Always On", or a $5 VPS):
  python satx_daily_pull.py

COST: ~100 listings/day on Haiku ≈ $0.05–0.10/day. $20 lasts months.
"""

import csv, json, os, re, sys, time
from datetime import date
import requests
from bs4 import BeautifulSoup

try:
    from anthropic import Anthropic
except ImportError:
    sys.exit("pip install anthropic")

# ----------------------------------------------------------------- CONFIG ----
ASSIGNMENT_FEE = 5000          # your fee in the MAO formula. $2,000 is your floor.
MAX_ASK        = 200_000       # ignore anything asking above this
MODEL          = "claude-haiku-4-5-20251001"

# Zillow zip average home value, pulled 2026-09-20. UPDATE MONTHLY.
# A renovated comp runs above these; a teardown runs below. These are anchors only.
ZIP_ARV = {
    "78201":171926, "78202":157057, "78203":161820, "78204":168484,
    "78207":109513, "78208":183620, "78210":162711, "78212":281983,
    "78213":203928, "78225":124045, "78226":135190, "78227":165930,
    "78228":169995, "78229":186501, "78237":127384, "78238":221480,
}

# Rehab line items ($). Used when the listing states the damage.
REHAB = {
    "roof":12000, "hvac":8000, "foundation":15000, "plumbing":10000,
    "electrical":10000, "kitchen":15000, "bath":7000, "fire":40000,
    "flooring":5000, "paint":4000, "windows":6000, "water_heater":1500,
}
COSMETIC_BASELINE = 15000      # paint/floors/fixtures when nothing is stated

KILL_WORDS = [
    "mobile home","manufactured","singlewide","single wide","doublewide","double wide",
    "park model","trailer","lot rent","approved by the park","rv ","tiny home",
    "for rent","apartment","lease","acres","acreage","lot for sale","land for sale",
    "owner finance","owner financing","dueno a dueno","dueño","no credit check",
    "realtor","brokerage","trec #","listing agent","mls#",
]
SKIP_POSTERS = ["inverterra"]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
client = Anthropic()

# --------------------------------------------------------------- SOURCES ----
def craigslist():
    """Craigslist SA real-estate-by-owner. purveyor=owner is REQUIRED."""
    url = "https://sanantonio.craigslist.org/search/rea?purveyor=owner"
    out = []
    try:
        soup = BeautifulSoup(requests.get(url, headers=UA, timeout=30).text, "html.parser")
        for a in soup.select("a[href*='/rea/'], li.cl-static-search-result a"):
            title = a.get_text(" ", strip=True)
            href = a.get("href")
            if href and title:
                out.append({"source":"craigslist","title":title,"url":href})
    except Exception as e:
        print("craigslist failed:", e)
    return out

def fsbo_com():
    url = "https://fsbo.com/search/list/san-antonio-tx"
    out = []
    try:
        soup = BeautifulSoup(requests.get(url, headers=UA, timeout=30).text, "html.parser")
        for a in soup.select("a[href*='/search/']"):
            t = a.get_text(" ", strip=True)
            if re.search(r"\$[\d,]+", t):
                out.append({"source":"fsbo.com","title":t,"url":a.get("href")})
    except Exception as e:
        print("fsbo.com failed:", e)
    return out

def fetch_body(url):
    try:
        r = requests.get(url, headers=UA, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup(["script","style","nav","footer"]): s.decompose()
        return soup.get_text(" ", strip=True)[:6000]
    except Exception:
        return ""

# --------------------------------------------------------------- CLAUDE -----
EXTRACT_PROMPT = """You are screening a real-estate listing for a wholesaler. Return ONLY JSON, no prose, no code fences.
Fields:
 property_type: "sfr" | "mobile" | "condo" | "multi" | "land" | "commercial" | "rental" | "unknown"
 is_agent_listed: true|false   (any realtor, brokerage, TREC, MLS mention = true)
 is_owner_finance: true|false  (seller wants terms, not a cash discount)
 ask: integer dollars or null
 beds: int|null, baths: float|null, sqft: int|null, zip: "5 digits"|null
 damage: list from [roof,hvac,foundation,plumbing,electrical,kitchen,bath,fire,flooring,paint,windows,water_heater]
 motivation: one short phrase (price cuts, must sell, as-is, inherited, tenant, code, vacant...) or ""
 days_or_history: short phrase if the listing shows age/price history, else ""
Listing:
"""

def extract(body):
    msg = client.messages.create(
        model=MODEL, max_tokens=400,
        messages=[{"role":"user","content":EXTRACT_PROMPT + body}],
    )
    txt = "".join(b.text for b in msg.content if getattr(b,"type","")=="text")
    txt = re.sub(r"```json|```","",txt).strip()
    try:
        return json.loads(txt)
    except Exception:
        return {}

# --------------------------------------------------------------- MAO --------
def mao(zipc, damage):
    arv = ZIP_ARV.get(zipc)
    if not arv:
        return None, None, None
    rehab = sum(REHAB[d] for d in damage if d in REHAB) or COSMETIC_BASELINE
    return arv, rehab, round(0.70*arv - rehab - ASSIGNMENT_FEE)

# --------------------------------------------------------------- MAIN -------
def main():
    today = date.today().isoformat()
    raw = craigslist() + fsbo_com()
    seen, listings = set(), []
    for l in raw:
        if l["url"] in seen: continue
        seen.add(l["url"]); listings.append(l)
    print(f"{len(listings)} listings pulled")

    survivors, killed = [], []
    for l in listings:
        t = l["title"].lower()
        if any(k in t for k in KILL_WORDS) or any(p in t for p in SKIP_POSTERS):
            killed.append({**l,"reason":"title kill-word"}); continue
        body = fetch_body(l["url"]); time.sleep(1.5)
        if not body:
            killed.append({**l,"reason":"no body"}); continue
        if any(p in body.lower() for p in SKIP_POSTERS):
            killed.append({**l,"reason":"skip poster"}); continue
        d = extract(body)
        if not d:
            killed.append({**l,"reason":"parse failed"}); continue
        if d.get("property_type") != "sfr":
            killed.append({**l,"reason":f"type={d.get('property_type')}"}); continue
        if d.get("is_agent_listed"):
            killed.append({**l,"reason":"agent listed"}); continue
        if d.get("is_owner_finance"):
            killed.append({**l,"reason":"owner-finance retail"}); continue
        ask = d.get("ask") or 0
        if ask > MAX_ASK:
            killed.append({**l,"reason":f"ask {ask} > max"}); continue
        arv, rehab, m = mao(d.get("zip"), d.get("damage") or [])
        row = {**l, **d, "arv":arv, "rehab":rehab, "mao":m,
               "gap": (m - ask) if (m and ask) else None}
        if m is None:
            row["reason"]="no ARV for zip — verify by hand"; survivors.append(row)  # keep, flag
        elif ask and ask <= m:
            row["reason"]="CLEARS MAO"; survivors.append(row)
        elif ask and ask <= m*1.10:
            row["reason"]="within 10% of MAO — offer at MAO"; survivors.append(row)
        else:
            row["reason"]=f"fails MAO by {ask-m}"; killed.append(row)

    def dump(name, rows):
        if not rows: return
        keys = sorted({k for r in rows for k in r})
        with open(name,"w",newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    dump(f"survivors_{today}.csv", survivors)
    dump(f"killed_{today}.csv", killed)

    print(f"\n=== {today}: {len(survivors)} survivors / {len(killed)} killed ===")
    for r in sorted(survivors, key=lambda x: (x.get("gap") or -1e9), reverse=True):
        print(f"[{r['reason']}] {r.get('title','')[:60]} | ask {r.get('ask')} | MAO {r.get('mao')} | {r.get('damage')} | {r['url']}")

if __name__ == "__main__":
    main()
