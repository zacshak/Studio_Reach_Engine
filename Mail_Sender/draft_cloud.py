"""Cloud cold-mail drafter: for every accepted lead awaiting a draft (Mail_status
'Writing') that has no mail yet, the configured vision model fills a template — personalising the
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
from draft_runtime import ModelOutputError, ProviderUnavailable, configured_client, infer, preflight

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
import pipeline      # noqa: E402
import media_store   # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TEMPLATES_FILE = os.path.join(HERE, "cold_mail_templates.txt")   # local fallback / seed
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
    name = next((f for f in images if f == 'SpriteSheet_Screenshots.png'), None)
    if not name:
        raise ValueError('incomplete media manifest: sprite sheet was not published')
    data = media_store.fetch_bytes(folder, name, strict=True)
    if not data or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('sprite sheet is empty or not a PNG')
    return base64.b64encode(data).decode()


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
    values = infer(client, model, msgs, lambda raw: _parse_observations(raw, len(slots)))
    return _render_template(template, game, studio, values)


def _complete_draft(appid, template, manifest):
    """Retry only the idempotent DB completion, never inference or R2 publication."""
    submission = manifest.get('draft_submission')
    if submission:
        media_store.check_draft_inputs(submission)
    for attempt in range(4):
        try:
            pipeline.complete_saved_draft(appid, template, submission)
            return
        except Exception as error:
            if not pipeline.retryable_db_error(error) or attempt == 3:
                raise
            pipeline.reconnect()
            delay = 2 ** attempt
            print(f'{appid} -> DB completion retry {attempt + 1}/3 in {delay}s: {type(error).__name__}', flush=True)
            time.sleep(delay)


def main():
    if '--check-only' not in sys.argv and '--preview-appid' not in sys.argv:
        if not pipeline.mail_status_appids('Writing'):
            print('No leads in Writing. Nothing to draft.', flush=True)
            return 0
    try:
        client, model = configured_client()
        preflight(client, model)
    except Exception as error:
        print(f'Drafting stopped before processing leads: {error}', file=sys.stderr, flush=True)
        return 1
    if '--preview-appid' in sys.argv:
        appid = int(sys.argv[sys.argv.index('--preview-appid') + 1])
        index = media_store.fetch_index(strict=True)
        folder = index.get(str(appid))
        if not folder:
            raise ValueError('Preview game has no indexed media')
        manifest = media_store.fetch_manifest(folder, strict=True)
        manifest['__template'] = _load_templates()[0][1]
        result = _draft(client, model, manifest, folder)
        print(f'Real-game preview passed for {appid}: {len(result)} characters. No draft saved and no database status changed.', flush=True)
        return 0
    if '--check-only' in sys.argv:
        return 0
    if not media_store.write_enabled():
        sys.exit("R2 write creds missing — can't write drafts to the manifest.")
    return run_queue(client, model)


def run_queue(client, model):
    templates = _load_templates()
    if not templates:
        sys.exit("no valid '#Cold Mail - N' templates found")
    index = media_store.fetch_index(strict=True)
    queue = pipeline.mail_status_appids("Writing")
    print(f"{len(queue)} lead(s) in 'Writing'; {len(templates)} templates")

    drafted = skipped = consecutive_failures = 0
    stopped = False
    deadline = time.monotonic() + 330 * 60
    tally = {n: 0 for n, _ in templates}
    for appid in queue:
        if time.monotonic() + 3 * 60 + 5 >= deadline:
            print('Stopping before the workflow deadline. Remaining leads retain Writing.', flush=True)
            stopped = True
            break
        folder = index.get(str(appid))
        try:
            manifest = media_store.fetch_manifest(folder, strict=True) if folder else None
        except Exception as error:
            print(f'{appid} -> media unavailable ({type(error).__name__}); retained for retry', flush=True)
            skipped += 1
            continue
        if not manifest:
            skipped += 1
            continue
        if (manifest.get("mail") or "").strip():
            # Recover a crash after the manifest write but before the DB transition.
            n = manifest.get("mail_template")
            try:
                _complete_draft(appid, n, manifest)
            except Exception as error:
                print(f'{appid} -> saved draft recovery deferred: {error}', flush=True)
                skipped += 1
                continue
            drafted += 1
            if n in tally:
                tally[n] += 1
            continue
        # pick templates in sequence (1,2,3,4,1,...) by how many we've drafted so far
        n, template = templates[drafted % len(templates)]
        manifest["__template"] = template
        try:
            mail = _draft(client, model, manifest, folder)
        except ProviderUnavailable as error:
            consecutive_failures += 1
            skipped += 1
            print(f'{appid} -> provider failure: {error}', file=sys.stderr, flush=True)
            if error.fatal or consecutive_failures >= 3:
                stopped = True
                print('Provider circuit open. Stopping this run; remaining leads retain Writing.', file=sys.stderr, flush=True)
                break
            continue
        except ModelOutputError as error:
            consecutive_failures = 0  # A model answered; this is not a provider outage.
            skipped += 1
            print(f'{appid} -> output validation failed: {error}; retained for retry', file=sys.stderr, flush=True)
            continue
        except Exception as e:
            print(f"  {appid} -> draft failed ({type(e).__name__}: {e})", file=sys.stderr)
            skipped += 1
            continue
        consecutive_failures = 0
        manifest.pop("__template", None)
        manifest["mail"] = mail
        manifest["mail_template"] = n
        try:
            persisted = media_store.write_manifest(folder, manifest)
            _complete_draft(appid, n, persisted if isinstance(persisted, dict) else manifest)
        except Exception as error:
            print(f'{appid} -> draft persistence deferred: {error}', flush=True)
            skipped += 1
            continue
        drafted += 1
        tally[n] += 1
        print(f"  {appid} {manifest.get('name', '')!r:.40} -> drafted (template {n})")

    print(f"done: {drafted} drafted, {skipped} skipped")
    print(", ".join(f"cold_mail_{n}: {tally[n]}" for n in sorted(tally)))
    print(f'Provider halted: {stopped}. Failed/unprocessed leads remain eligible for retry.', flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(f'\nDrafting: {drafted} completed/recovered, {skipped} skipped/failed. Provider/deadline stop: {stopped}. Initial queue: {len(queue)}.\n')
    return 1 if skipped or stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
