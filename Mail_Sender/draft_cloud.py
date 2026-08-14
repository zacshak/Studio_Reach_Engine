"""Cloud cold-mail drafter: for every accepted lead awaiting a draft (Mail_status
'Writing') that has no mail yet, Gemini fills one of the 4 templates — personalising the
critique/observation slot from the game's sprite sheet + blurb — and the finished mail is
written into the lead's R2 manifest ('mail' + 'mail_template'), then Mail_status flips to
'Drafted'. The review app's Mail-Approval section only shows 'Drafted' leads; Approve ->
Scheduled -> the send job mails it.

Runs as a GHA step, fired on demand by the app's "Draft pending mails" button. Idempotent:
re-running only drafts leads whose manifest has no mail yet, so a half-finished run resumes.

Env: TURSO_* (read the 'Writing' queue) + R2 read/write (manifest) + TRIAGE_BASE_URL/MODEL/
API_KEY (reused Gemini creds; the model must be vision-capable for the sheet).
"""
import base64
import os
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

TEMPLATES_FILE = os.path.join(HERE, "cold_mail_templates.txt")   # local fallback / seed
RETRIES = 6
_UA = {"User-Agent": "Mozilla/5.0"}


def _parse_templates(raw):
    """The templates as a list of (n, text), split on the '#Cold Mail - N' headers,
    in ascending number order (so selection cycles 1,2,3,4,1,...)."""
    parts = re.split(r"#Cold Mail\s*-\s*(\d+)", raw)
    return sorted((int(parts[i]), parts[i + 1].strip().strip("-").strip())
                  for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip())


def _normalize_terms(text):
    """Undo speech-style rewrites of technical names and numeric '+' notation."""
    text = re.sub(r"\bC\s+plus\s+plus\b", "C++", text, flags=re.I)
    text = re.sub(r"\bC\s+sharp\b", "C#", text, flags=re.I)
    return re.sub(r"\b(\d+)\s+plus\b", r"\1+", text, flags=re.I)


def _load_templates():
    """Templates from R2 (the persistent source of truth); fall back to the tracked file."""
    raw = media_store.fetch_templates(strict=True)
    if not raw:
        raw = open(TEMPLATES_FILE, encoding="utf-8").read()
    return _parse_templates(raw)


PROMPT = """You are a Cold Mail writer. I pitch my game programming skills as a freelance/
contract service. Analyse ONE game and write a cold email to its studio.

Game: "{game}"  |  studio/devs: {devs}

The MOST IMPORTANT context is the attached sprite-sheet screenshot image — study it; it gives
the best read on the game. Fill in the placeholders of THIS template for this specific game:

TEMPLATE:
{template}

Rules:
- Replace <game>/<game name> with the game's name and <developer/studio name> with the studio.
- Write the <...> critique/observation slot yourself.
- EVERY concrete claim in the mail must come from what is ACTUALLY VISIBLE in the sprite sheet.
  Never state store-page features as if you saw them. If the screenshot shows no flaw, critique
  only what is on screen, or do not critique at all. Never invent facts.
- Do NOT use the '-' character anywhere in what you write; it screams AI written. Reword instead.
- Preserve technical names and symbols exactly: C++, C#, DirectX 12, OpenGL, Unreal Engine,
  Unity, AAA, and numeric forms such as 4+ and 30+.
- Keep the "Subject:" line and the template's tone. Output ONLY the finished email, nothing else."""


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
        template=manifest["__template"])}]
    b64 = _sheet_b64(folder, manifest.get("images", []))
    if b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    msgs = [{"role": "user", "content": content}]
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(model=model, max_tokens=600, messages=msgs)
            mail = _normalize_terms((resp.choices[0].message.content or "").strip())
            if not mail:
                raise ValueError("model returned an empty draft")
            return mail
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

    templates = _load_templates()
    if not templates:
        sys.exit("no valid '#Cold Mail - N' templates found")
    index = media_store.fetch_index(strict=True)
    queue = pipeline.mail_status_appids("Writing")
    print(f"{len(queue)} lead(s) in 'Writing'; {len(templates)} templates")

    drafted = skipped = 0
    tally = {n: 0 for n, _ in templates}
    for appid in queue:
        folder = index.get(str(appid))
        manifest = media_store.fetch_manifest(folder, strict=True) if folder else None
        if not manifest:
            skipped += 1
            continue
        if (manifest.get("mail") or "").strip():
            # Recover a crash after the manifest write but before the DB transition.
            n = manifest.get("mail_template")
            if n is not None:
                pipeline.set_mail_template(appid, n)
            pipeline.set_mail_status(appid, "Drafted")
            drafted += 1
            if n in tally:
                tally[n] += 1
            continue
        # pick templates in sequence (1,2,3,4,1,...) by how many we've drafted so far
        n, template = templates[drafted % len(templates)]
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
        pipeline.set_mail_template(appid, n)
        pipeline.set_mail_status(appid, "Drafted")
        drafted += 1
        tally[n] += 1
        print(f"  {appid} {manifest.get('name', '')!r:.40} -> drafted (template {n})")

    print(f"done: {drafted} drafted, {skipped} skipped")
    print(", ".join(f"cold_mail_{n}: {tally[n]}" for n in sorted(tally)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
