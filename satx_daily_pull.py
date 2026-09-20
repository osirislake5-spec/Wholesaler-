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
  - Zillow (FSBO section) and Redfin are attempted with a headless browser
    (Playwright/Chromium). CONFIRMED LIVE: both front their sites with
    Akamai/CloudFront-class bot management that blocks plain headless
    Chromium outright (403 / "access denied" interstitial) from most
    datacenter and VPS IPs, before any listing content even loads. This is
    not a selector problem, and stealth patches are a losing arms race
    against Akamai/CloudFront. The script detects the block page and says
    so loudly in stderr ("zillow BLOCKED" / "redfin BLOCKED") instead of
    quietly reporting 0 rows as if there were no listings.
    THE FIX: set SCRAPER_PROXY_URL to a residential/ISP proxy or a paid
    anti-bot scraping API (ScraperAPI, ZenRows, Bright Data — all sell a
    Playwright-compatible proxy endpoint, ~$30-75/mo starter tiers). Once
    set, zillow()/redfin() route through it automatically with no other
    code changes. Until you add one, treat Craigslist + FSBO.com as the
    reliable daily feed and pull Zillow/Redfin by hand.
  - It cannot get phone numbers. Contact route stays: FSBO.com form,
    Craigslist relay, DSD (210) 207-5422 for code cases, TruePeopleSearch.
  - Zip ARVs below are Zillow zip AVERAGES from 2026-09-20, not comps.
    Update them monthly. A comp from Harold beats any number in this table.

SETUP (once):
  pip install anthropic requests beautifulsoup4 playwright
  playwright install chromium               (skip on a box that already has it, e.g. PLAYWRIGHT_BROWSERS_PATH set)
  export ANTHROPIC_API_KEY=sk-ant-...     (your $20 of credits lives here)
  export SCRAPER_PROXY_URL=http://user:pass@host:port   (optional — required for zillow/redfin to actually get through)

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

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

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

# Zillow and Redfin front their sites with Akamai/CloudFront-class bot
# management that fingerprints headless Chromium and blocks most
# datacenter/VPS IPs before any page content loads — confirmed live: a
# plain headless Playwright run against both got a 403/"access denied"
# interstitial, not a selector mismatch. A DIY fix (stealth patches,
# fingerprint spoofing) is a losing arms race against Akamai/CloudFront.
# The reliable fix is routing through a residential/ISP proxy or a paid
# anti-bot scraping API (ScraperAPI, ZenRows, Bright Data all offer a
# Playwright-compatible proxy endpoint). Set SCRAPER_PROXY_URL to one and
# this code routes through it automatically; without it, expect these two
# sources to come back empty most days and the script says so loudly
# instead of pretending "0 results" means "no listings."
BLOCK_SIGNS = [
    "access to this page has been denied", "request could not be satisfied",
    "are you a robot", "captcha", "unusual traffic", "request blocked",
    "pardon our interruption", "verify you are a human",
]

def _looks_blocked(page):
    title = (page.title() or "").lower()
    try:
        body = (page.inner_text("body") or "")[:2000].lower()
    except Exception:
        body = ""
    return any(s in title or s in body for s in BLOCK_SIGNS)

def _chromium_launch(pw):
    """Use a pre-fetched Chromium under PLAYWRIGHT_BROWSERS_PATH if present
    (e.g. this sandbox); otherwise let Playwright resolve its own default
    install (a fresh VPS after `playwright install chromium`). Routes
    through SCRAPER_PROXY_URL (e.g. http://user:pass@host:port from a
    residential-proxy or anti-bot scraping API vendor) when set."""
    kwargs = {"headless": True}
    proxy_url = os.environ.get("SCRAPER_PROXY_URL")
    if proxy_url:
        kwargs["proxy"] = {"server": proxy_url}
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if pw_path:
        import glob
        matches = glob.glob(os.path.join(pw_path, "chromium-*", "chrome-linux", "chrome"))
        if matches:
            kwargs["executable_path"] = matches[0]
    return pw.chromium.launch(**kwargs)

def zillow():
    """Zillow FSBO section for San Antonio. Headless browser — Zillow blocks
    plain requests outright, and blocks bare headless Chromium too (see
    BLOCK_SIGNS note above). Needs SCRAPER_PROXY_URL to get through
    reliably from most hosting."""
    if not HAS_PLAYWRIGHT:
        print("zillow skipped: playwright not installed (pip install playwright && playwright install chromium)")
        return []
    out, url = [], "https://www.zillow.com/san-antonio-tx/fsbo/"
    try:
        with sync_playwright() as pw:
            browser = _chromium_launch(pw)
            page = browser.new_context(user_agent=UA["User-Agent"]).new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if _looks_blocked(page):
                print(f"zillow BLOCKED (bot check, page title={page.title()!r}) — "
                      "set SCRAPER_PROXY_URL to a residential proxy/scraping API "
                      "to get through. This is not '0 listings today'.")
                browser.close()
                return out
            seen = set()
            for a in page.query_selector_all("a[href*='/homedetails/']"):
                href = a.get_attribute("href")
                title = (a.inner_text() or "").strip()
                if not href or href in seen:
                    continue
                seen.add(href)
                full = href if href.startswith("http") else "https://www.zillow.com" + href
                out.append({"source":"zillow","title":title or full,"url":full})
            browser.close()
    except Exception as e:
        print("zillow failed:", e)
    return out

def redfin():
    """Redfin San Antonio listings. Headless browser — Redfin has no clean
    FSBO-only filter, so this pulls the general search and leans on the
    existing is_agent_listed check in main() to kill agent-represented
    listings during the Claude extraction pass. Also blocks bare headless
    Chromium at the CDN layer (see BLOCK_SIGNS note above) — needs
    SCRAPER_PROXY_URL to get through reliably from most hosting."""
    if not HAS_PLAYWRIGHT:
        print("redfin skipped: playwright not installed (pip install playwright && playwright install chromium)")
        return []
    out, url = [], "https://www.redfin.com/city/30819/TX/San-Antonio"
    try:
        with sync_playwright() as pw:
            browser = _chromium_launch(pw)
            page = browser.new_context(user_agent=UA["User-Agent"]).new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if _looks_blocked(page):
                print(f"redfin BLOCKED (bot check, page title={page.title()!r}) — "
                      "set SCRAPER_PROXY_URL to a residential proxy/scraping API "
                      "to get through. This is not '0 listings today'.")
                browser.close()
                return out
            seen = set()
            for a in page.query_selector_all("a[href*='/TX/San-Antonio/']"):
                href = a.get_attribute("href")
                title = (a.inner_text() or "").strip()
                if not href or "/home/" not in href or href in seen:
                    continue
                seen.add(href)
                full = href if href.startswith("http") else "https://www.redfin.com" + href
                out.append({"source":"redfin","title":title or full,"url":full})
            browser.close()
    except Exception as e:
        print("redfin failed:", e)
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
    raw = craigslist() + fsbo_com() + zillow() + redfin()
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
