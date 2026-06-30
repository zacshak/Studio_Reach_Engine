"""Hermes — the scraper brain (runs in GitHub Actions, triggered by the app's button).

Division of labour:
  - Playwright (headless Chromium) does the SCRAPING — renders each studio site and a
    few likely contact pages, and runs a keyless DuckDuckGo search when a lead has no
    website. This is the part that touches the web.
  - Gemini (the TRIAGE_* OpenAI-compatible endpoint) is the BRAIN — from the rendered
    text it extracts the single best recruiting-grade email per the priority rubric,
    de-obfuscating where needed, or says NONE. It never invents an address.

The DB is reached only through scraper_interface (get_pending / read_lead / write_result):
  email found  -> scrape_status='scraped'  (leaves No-Mail, surfaces in Game-Approval)
  none found   -> scrape_status='no_email'
  errored      -> scrape_status='failed'   (stays pending, retried next run)

Env: TURSO_* (via .env / GHA secrets, loaded by pipeline) + TRIAGE_BASE_URL/MODEL/API_KEY
(reused from triage so there's nothing new to configure). Playwright browser must be
installed (`playwright install chromium`).
"""
import os
import re
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scraper_interface as leads  # noqa: E402  (re-exports pipeline; also loads .env)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# pages worth chasing for a contact address, by url/link-text keyword
CONTACT_HINTS = ("contact", "about", "career", "job", "team", "press", "impressum", "legal")
MAX_PAGES = 6          # landing + up to this many contact-ish pages per lead
GOTO_TIMEOUT = 20_000  # ms per navigation; a slow/dead site shouldn't sink the run
RETRIES = 6            # Gemini call retries (free key gets rate-limited)


# -- Playwright scraping ---------------------------------------------------
def _text(page):
    """Visible text + raw HTML of the current page (HTML catches mailto:/cfemail that
    don't render as text). Truncated — we only need the contact-y bits."""
    try:
        body = page.inner_text("body", timeout=5_000)
    except Exception:
        body = ""
    html = page.content()
    return (body + "\n" + html)[:20_000]


def _same_site(base, href):
    try:
        return urllib.parse.urlparse(href).netloc.endswith(
            urllib.parse.urlparse(base).netloc.split(":")[0]) or href.startswith("/")
    except Exception:
        return False


def _scrape_site(page, url):
    """Render `url`, then follow up to a few same-site contact-ish links. Returns the
    concatenated text of every page we managed to load (empty string if none loaded)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    chunks, to_visit, seen = [], [url], set()
    while to_visit and len(seen) < MAX_PAGES:
        u = to_visit.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            page.goto(u, timeout=GOTO_TIMEOUT, wait_until="domcontentloaded")
        except Exception:
            continue
        chunks.append(f"--- {u} ---\n{_text(page)}")
        if len(seen) == 1:                        # from the landing page, queue contact pages
            try:
                hrefs = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)")
            except Exception:
                hrefs = []
            for h in hrefs:
                if (len(to_visit) + len(seen) < MAX_PAGES and _same_site(u, h)
                        and any(k in h.lower() for k in CONTACT_HINTS) and h not in seen):
                    to_visit.append(h)
    return "\n".join(chunks)


def _find_site(page, query):
    """No website on the lead — search DuckDuckGo's HTML endpoint (keyless) for the
    studio and return the first few result URLs to try. ponytail: HTML scrape of one
    search page; swap for a real search API if DDG ever changes its markup."""
    try:
        page.goto("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query),
                  timeout=GOTO_TIMEOUT, wait_until="domcontentloaded")
        hrefs = page.eval_on_selector_all(
            "a.result__a", "els => els.map(e => e.href)")
    except Exception:
        return []
    out = []
    for h in hrefs:
        # DDG wraps results in a redirect (uddg=<encoded real url>); unwrap it.
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("uddg")
        real = urllib.parse.unquote(q[0]) if q else h
        if real.startswith("http") and real not in out:
            out.append(real)
        if len(out) >= 3:
            break
    return out


# -- Gemini extraction (the brain) -----------------------------------------
RUBRIC = """You are an email-finder for a freelance game programmer pitching studios.
From the scraped website text below for the studio "{studio}" (game: "{game}"), return the
ONE best CONTACT email to reach a hiring/decision contact. Prefer, high to low:
a real person's name (jane@studio) > careers@/jobs@/hr@/recruiting@/talent@ >
hello@/info@/contact@/team@ > press@/business@ > support@/noreply@. Prefer the studio's
own domain. De-obfuscate forms like "name [at] studio [dot] com". NEVER invent an address
— only return one that literally appears in the text. If no usable email appears, return NONE.
Output ONLY the email address or the word NONE, nothing else.

SCRAPED TEXT:
{text}"""


def _pick_email(client, model, studio, game, text):
    """Ask Gemini for the single best email in `text` (or None). Retries on API error;
    validates the answer actually occurs in the text so a hallucinated address is dropped."""
    prompt = RUBRIC.format(studio=studio, game=game, text=text[:30_000])
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=40,
                messages=[{"role": "user", "content": prompt}])
            ans = (resp.choices[0].message.content or "").strip()
            m = EMAIL_RE.search(ans)
            if not m:
                return None
            email = m.group(0)
            # guard against fabrication: the address must appear in the scraped source
            return email if email.lower() in text.lower() else None
        except Exception as e:
            if attempt == RETRIES - 1:
                raise
            wait = min(60, 2 ** attempt) + attempt
            print(f"    gemini error ({type(e).__name__}), retry "
                  f"{attempt + 1}/{RETRIES} in {wait}s", file=sys.stderr)
            time.sleep(wait)


def _candidate_urls(lead):
    """Best site hints for a lead, in order: its website, then Steam's support url."""
    urls, support = [], lead.get("support_info") or ""
    if lead.get("website"):
        urls.append(lead["website"])
    m = re.search(r'"url"\s*:\s*"([^"]+)"', support)   # support_info is a raw JSON string
    if m and m.group(1) not in urls:
        urls.append(m.group(1))
    return [u for u in urls if u and "store.steampowered.com" not in u]


def main():
    base = os.environ.get("TRIAGE_BASE_URL")
    key = os.environ.get("TRIAGE_API_KEY")
    model = os.environ.get("TRIAGE_MODEL")
    if not (base and key and model):
        sys.exit("set TRIAGE_BASE_URL / TRIAGE_API_KEY / TRIAGE_MODEL")
    from openai import OpenAI
    from playwright.sync_api import sync_playwright
    client = OpenAI(base_url=base, api_key=key, max_retries=0)

    queue = leads.get_pending()
    if not queue:
        print("no pending leads — nothing to scrape")
        return 0
    print(f"scraping {len(queue)} pending lead(s)")

    found = nomail = failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Hermes lead scraper)")
        for appid in queue:
            try:
                lead = leads.read_lead(appid) or {}
                studio = lead.get("game_name") or lead.get("name") or str(appid)
                urls = _candidate_urls(lead)
                if not urls:
                    urls = _find_site(page, f'"{studio}" game studio official site')
                text = "\n".join(_scrape_site(page, u) for u in urls[:3])
                email = _pick_email(client, model, studio, studio, text) if text.strip() else None
                if email:
                    leads.write_result(appid, scrape_status="scraped", emails=email,
                                       website=urls[0] if urls else None)
                    found += 1
                    print(f"  {appid} {studio!r:.40} -> {email}")
                else:
                    leads.write_result(appid, scrape_status="no_email")
                    nomail += 1
                    print(f"  {appid} {studio!r:.40} -> no_email")
            except Exception as e:
                leads.write_result(appid, scrape_status="failed")
                failed += 1
                print(f"  {appid} -> failed ({type(e).__name__}: {e})", file=sys.stderr)
        browser.close()

    print(f"done: {found} scraped, {nomail} no_email, {failed} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
