"""Sequential social enrichment for scrape_tracker rows explicitly requested by users."""
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hermes  # noqa: E402
import scraper_interface as leads  # noqa: E402

FIELDS = ("X", "LinkedIn", "Instagram", "Discord", "Email")
DOMAINS = {
    "X": ("x.com", "twitter.com"),
    "LinkedIn": ("linkedin.com",),
    "Instagram": ("instagram.com",),
    "Discord": ("discord.gg", "discord.com"),
}
PROMPT = """Find the official social/contact details for this game studio from the
provided game row and web-search evidence. Return ONLY one JSON object with exactly
these keys: X, LinkedIn, Instagram, Discord, Email. Values must be an exact URL/email
present in the evidence; use null when not found. Never infer or invent a value.

GAME ROW:
{row}

WEB EVIDENCE:
{evidence}
"""


def _search(page, query):
    """Return DuckDuckGo results, or None when the search itself could not run."""
    try:
        page.goto("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query),
                  timeout=hermes.GOTO_TIMEOUT, wait_until="domcontentloaded")
        results = page.eval_on_selector_all(
            "a.result__a", "els => els.map(e => ({text: e.innerText, href: e.href}))")
    except Exception:
        return None
    lines = []
    for result in results[:8]:
        href = result.get("href") or ""
        wrapped = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        url = urllib.parse.unquote(wrapped[0]) if wrapped else href
        if url.startswith(("http://", "https://")):
            lines.append(f"{result.get('text', '').strip()}\n{url}")
    return "\n".join(lines)


def _valid(field, value, evidence):
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.casefold() not in evidence.casefold():
        return None
    if field == "Email":
        match = hermes.EMAIL_RE.fullmatch(value)
        return match.group(0) if match else None
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if parsed.scheme not in ("http", "https"):
        return None
    return value if any(host == domain or host.endswith("." + domain)
                        for domain in DOMAINS[field]) else None


def _parse(answer, evidence):
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return JSON")
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model JSON is not an object")
    return {field: _valid(field, data.get(field), evidence) for field in FIELDS}


def _extract(client, model, row, evidence):
    prompt = PROMPT.format(
        row=json.dumps(row, ensure_ascii=False, default=str), evidence=evidence[:50_000])
    for attempt in range(hermes.RETRIES):
        try:
            response = client.chat.completions.create(
                model=model, max_tokens=300,
                messages=[{"role": "user", "content": prompt}])
            return _parse(response.choices[0].message.content or "", prompt)
        except Exception as exc:
            if attempt == hermes.RETRIES - 1:
                raise
            wait = min(60, 2 ** attempt) + attempt
            print(f"    social extractor error ({type(exc).__name__}), retry "
                  f"{attempt + 1}/{hermes.RETRIES} in {wait}s", file=sys.stderr)
            time.sleep(wait)


def _evidence(page, row):
    game = row.get("game_name") or str(row["appid"])
    studio = ((row.get("developers") or row.get("publishers") or game).split(",", 1)[0]
              .strip())
    site_text = "\n".join(
        hermes._scrape_site(page, url) for url in hermes._candidate_urls(row)[:3])
    search_results = [_search(page, query) for query in (
        f'"{studio}" "{game}" (site:x.com OR site:twitter.com)',
        f'"{studio}" "{game}" site:linkedin.com',
        f'"{studio}" "{game}" site:instagram.com',
        f'"{studio}" "{game}" (site:discord.gg OR site:discord.com/invite)',
        f'"{studio}" "{game}" contact email',
    )]
    if not site_text and all(result is None for result in search_results):
        raise RuntimeError("no web source could be reached")
    searches = "\n".join(result or "" for result in search_results)
    return f"OFFICIAL SITE\n{site_text}\nSEARCH RESULTS\n{searches}"


def main():
    base = os.environ.get("TRIAGE_BASE_URL")
    key = os.environ.get("TRIAGE_API_KEY")
    model = os.environ.get("TRIAGE_MODEL")
    if not (base and key and model):
        sys.exit("set TRIAGE_BASE_URL / TRIAGE_API_KEY / TRIAGE_MODEL")
    from openai import OpenAI
    from playwright.sync_api import sync_playwright

    queue = leads.social_requests()
    if not queue:
        print("no requested socials — nothing to enrich")
        return 0
    print(f"enriching {len(queue)} requested lead(s) sequentially")
    client = OpenAI(base_url=base, api_key=key, max_retries=0)
    completed = failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Hermes social scraper)")
        for row in queue:
            appid = int(row["appid"])
            try:
                result = _extract(client, model, row, _evidence(page, row))
                if leads.write_socials(appid, result):
                    completed += 1
                    found = ", ".join(k for k, value in result.items() if value) or "none"
                    print(f"  {appid} -> {found}")
            except Exception as exc:
                failed += 1
                print(f"  {appid} -> failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        browser.close()
    print(f"done: {completed} enriched, {failed} failed (left requested for retry)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
