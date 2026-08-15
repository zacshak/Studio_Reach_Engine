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
import json
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
_PLACEHOLDER = re.compile(r"<([^<>]+)>")
_GAME_SLOTS = {"game", "game name"}
_STUDIO_SLOTS = {"developer/studio name"}


def _parse_templates(raw):
    """The templates as a list of (n, text), split on the '#Cold Mail - N' headers,
    in ascending number order (so selection cycles 1,2,3,4,1,...)."""
    parts = re.split(r"#Cold Mail\s*-\s*(\d+)", raw)
    return sorted((int(parts[i]), parts[i + 1].strip().strip("-").strip())
                  for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip())


def _normalize_terms(text):
    """Undo speech-style rewrites inside one model-generated value."""
    text = re.sub(r"\bC\s+plus\s+plus\b", "C++", text, flags=re.I)
    text = re.sub(r"\bC\s+sharp\b", "C#", text, flags=re.I)
    return re.sub(r"\b(\d+)\s+plus\b", r"\1+", text, flags=re.I)


def _load_templates():
    """Templates from R2 (the persistent source of truth); fall back to the tracked file."""
    raw = media_store.fetch_templates(strict=True)
    if not raw:
        raw = open(TEMPLATES_FILE, encoding="utf-8").read()
    return _parse_templates(raw)


SYSTEM_PROMPT = """You write personalized observation values for a cold-email template.
Return only one valid JSON object with exactly the requested keys. Never return the email,
the template, Markdown fences, commentary, or additional keys."""


PROMPT = """I pitch my game programming skills as a freelance/contract service. Analyse ONE
game and fill only the requested observation slots.

Game: "{game}"  |  studio/devs: {devs}

The MOST IMPORTANT context is the attached sprite-sheet screenshot image — study it; it gives
the best read on the game.

READ-ONLY TEMPLATE CONTEXT:
{template}

Return this JSON shape:
{json_shape}

Slot meanings:
{slot_meanings}

Rules:
- Write one string value for every requested observation key.
- EVERY concrete claim in the mail must come from what is ACTUALLY VISIBLE in the sprite sheet.
  Never state store-page features as if you saw them. If the screenshot shows no flaw, critique
  only what is on screen, or do not critique at all. Never invent facts.
- Do not use the '-' character in observation values. Reword instead.
- Preserve technical names and symbols exactly: C++, C#, DirectX 12, OpenGL, Unreal Engine,
  Unity, AAA, and numeric forms such as 4+ and 30+.
- Match the surrounding template tone. Return only the JSON object."""


def _studio_name(manifest):
    """Developer segment from the legacy '<developers> · <genres>' card metadata."""
    return (manifest.get("meta") or "").split("·", 1)[0].strip() or "there"


def _observation_slots(template):
    return [label.strip() for label in _PLACEHOLDER.findall(template)
            if label.strip().lower() not in _GAME_SLOTS | _STUDIO_SLOTS]


def _parse_observations(raw, count):
    """Parse and validate the model's observation-only JSON response."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return a JSON object")
    data = json.loads(text[start:end + 1])
    keys = [f"observation_{i}" for i in range(1, count + 1)]
    if not isinstance(data, dict) or set(data) != set(keys):
        raise ValueError(f"model must return exactly {keys}")
    values = []
    for key in keys:
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        value = _normalize_terms(value.strip())
        if "<" in value or ">" in value or "-" in value:
            raise ValueError(f"{key} contains a forbidden character")
        values.append(value)
    return values


def _render_template(template, game, studio, observations):
    """Replace known slots while preserving every literal template byte."""
    observations = iter(observations)

    def replace(match):
        label = match.group(1).strip().lower()
        if label in _GAME_SLOTS:
            return game
        if label in _STUDIO_SLOTS:
            return studio
        return next(observations)

    try:
        rendered = _PLACEHOLDER.sub(replace, template)
    except StopIteration as exc:
        raise ValueError("missing observation value") from exc
    try:
        next(observations)
    except StopIteration:
        return rendered
    raise ValueError("extra observation value")


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
    """One vision-model call for slot values, then deterministic template rendering."""
    template = manifest["__template"]
    game = manifest.get("name", "")
    studio = _studio_name(manifest)
    slots = _observation_slots(template)
    if not slots:
        return _render_template(template, game, studio, [])
    b64 = _sheet_b64(folder, manifest.get("images", []))
    if not b64:
        raise ValueError("sprite sheet unavailable")
    keys = [f"observation_{i}" for i in range(1, len(slots) + 1)]
    content = [{"type": "text", "text": PROMPT.format(
        game=game,
        devs=studio,
        template=template,
        json_shape=json.dumps(dict.fromkeys(keys, "...")),
        slot_meanings="\n".join(f"- {key}: {label}" for key, label in zip(keys, slots)),
    )}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content}]
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(model=model, max_tokens=600, messages=msgs)
            values = _parse_observations(resp.choices[0].message.content, len(slots))
            return _render_template(template, game, studio, values)
        except Exception as e:
            if attempt == RETRIES - 1:
                raise
            wait = min(60, 2 ** attempt) + attempt
            print(f"    mail-writer error ({type(e).__name__}), retry "
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
