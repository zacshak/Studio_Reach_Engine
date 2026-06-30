"""Cloud cold-mail drafter: for every accepted lead awaiting a draft (Mail_status
'Writing') that has no mail yet, Gemini fills one of the 4 templates — personalising the
critique/observation slot from the game's sprite sheet + blurb — and the finished mail is
written into the lead's R2 manifest ('mail' + 'mail_template'). The review app's
Mail-Approval section then shows it; Approve -> Scheduled -> the send job mails it.

Runs as a GHA step, fired on demand by the app's "Draft pending mails" button. Idempotent:
re-running only drafts leads whose manifest has no mail yet, so a half-finished run resumes.

Env: TURSO_* (read the 'Writing' queue) + R2 read/write (manifest) + TRIAGE_BASE_URL/MODEL/
API_KEY (reused Gemini creds; the model must be vision-capable for the sheet).
"""
import base64
import os
import random
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
import pipeline      # noqa: E402
import media_store   # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TEMPLATES_FILE = os.path.join(HERE, "cold_mail_templates.txt")
RETRIES = 6
_UA = {"User-Agent": "Mozilla/5.0"}


def _templates():
    """The 4 templates as a list of (n, text), split on the '#Cold Mail - N' headers."""
    raw = open(TEMPLATES_FILE, encoding="utf-8").read()
    out = []
    for block in re.split(r"#Cold Mail\s*-\s*(\d+)", raw)[1:]:
        if out and out[-1][1] is None:
            out[-1] = (out[-1][0], block.strip().strip("-").strip())
        else:
            out.append((int(block), None))
    return [(n, t) for n, t in out if t]


PROMPT = """You are drafting a cold outreach email for Meshak, a freelance game programmer
pitching his services to the studio behind the game "{game}" (studio/devs: {devs}).
Game blurb: {desc}

Fill in THIS template exactly — keep its tone, structure, and the "Subject:" line. Replace
<game>/<game name> with the game's name, <developer/studio name> with the studio, and write
the <...> critique/observation slot yourself: ONE or TWO genuine, specific lines based on the
attached sprite-sheet image and the blurb (a real mechanic, the art, the atmosphere, a rough
edge) — never generic flattery, never invented facts. Output ONLY the finished email, nothing
else.

TEMPLATE:
{template}"""


def _sheet_b64(folder, images):
    """Base64 of the lead's sprite sheet from R2 (for the visual observation), or None."""
    name = next((f for f in images if "SpriteSheet" in f), None)
    if not name:
        return None
    try:
        req = urllib.request.Request(media_store.public_url(folder, name), headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return base64.b64encode(r.read()).decode()
    except Exception:
        return None


def _draft(client, model, manifest, folder):
    """One Gemini call -> the finished mail text for this lead."""
    content = [{"type": "text", "text": PROMPT.format(
        game=manifest.get("name", ""), devs=manifest.get("meta", ""),
        desc=(manifest.get("desc") or "")[:600],
        template=manifest["__template"])}]
    b64 = _sheet_b64(folder, manifest.get("images", []))
    if b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    msgs = [{"role": "user", "content": content}]
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(model=model, max_tokens=600, messages=msgs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            if attempt == RETRIES - 1:
                raise
            wait = min(60, 2 ** attempt) + attempt
            print(f"    gemini error ({type(e).__name__}), retry "
                  f"{attempt + 1}/{RETRIES} in {wait}s", file=sys.stderr)
            time.sleep(wait)


def main():
    base = os.environ.get("TRIAGE_BASE_URL")
    key = os.environ.get("TRIAGE_API_KEY")
    model = os.environ.get("TRIAGE_MODEL")
    if not (base and key and model):
        sys.exit("set TRIAGE_BASE_URL / TRIAGE_API_KEY / TRIAGE_MODEL")
    if not media_store.write_enabled():
        sys.exit("R2 write creds missing — can't write drafts to the manifest.")
    from openai import OpenAI
    client = OpenAI(base_url=base, api_key=key, max_retries=0)

    templates = _templates()
    index = media_store.fetch_index()
    queue = pipeline.mail_status_appids("Writing")
    print(f"{len(queue)} lead(s) in 'Writing'")

    drafted = skipped = 0
    for appid in queue:
        folder = index.get(str(appid))
        manifest = media_store.fetch_manifest(folder) if folder else None
        if not manifest:
            skipped += 1
            continue
        if (manifest.get("mail") or "").strip():     # already drafted — idempotent
            skipped += 1
            continue
        n, template = random.choice(templates)
        manifest["__template"] = template
        try:
            mail = _draft(client, model, manifest, folder)
        except Exception as e:
            print(f"  {appid} -> draft failed ({type(e).__name__}: {e})", file=sys.stderr)
            skipped += 1
            continue
        manifest.pop("__template", None)
        manifest["mail"] = mail
        manifest["mail_template"] = n
        media_store.write_manifest(folder, manifest)
        drafted += 1
        print(f"  {appid} {manifest.get('name', '')!r:.40} -> drafted (template {n})")

    print(f"done: {drafted} drafted, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
