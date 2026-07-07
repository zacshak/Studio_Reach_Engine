"""Cloud triage: a VISION model reviews the current Pending leads from Turso and
flags irrelevant appids into R2's irrelevant.json. It reads each lead's manifest and
sprite sheet from R2, so this can run independently or be retried manually.
NON-destructive — the human Accepts/Rejects in the app.

Provider-agnostic (OpenAI-compatible). Set:
    TRIAGE_BASE_URL   e.g. https://api.moonshot.cn/v1  (must be a VISION endpoint)
    TRIAGE_MODEL      a vision-capable model id
    TRIAGE_API_KEY
Plus the R2 write creds (so it can write irrelevant.json). See media_store.py.
"""
import base64
import os
import random
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pipeline  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "Leads_Reviewer"))
import media_store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BATCH = 4                         # games (= images) per call; keeps appid<->image mapping clean
_UA = {"User-Agent": "Mozilla/5.0"}

RUBRIC = """You are a Games Reviewer/filterer for a freelancer who pitches game-programming
to studios that would benefit from hiring a developer. For EACH game below, use its JSON and
— most importantly — its sprite-sheet image (the sheet is the best read on the game; analyse
it per game). REJECT a game if it is any of: a desktop app/utility; Dating Sim / Romance;
Visual Novel / Interactive Fiction; a 2D game; something a single developer could clearly
build alone; or AI slop. ALLOW everything else. Output ONE line, nothing else, listing the
rejected appids from THIS set {ids}:
Rejected Games = [4858620, 4819850]"""


def _games():
    """Resolve Pending scrape_tracker rows to (appid, folder, manifest) records."""
    appids = pipeline.mail_status_appids("Pending")
    if not appids:
        return []
    index = media_store.fetch_index()
    out = []
    missing = []
    for appid in appids:
        folder = index.get(str(appid))
        manifest = media_store.fetch_manifest(folder) if folder else None
        if not folder or not manifest:
            missing.append(appid)
            continue
        out.append((appid, folder, manifest))
    if missing:
        raise RuntimeError(f"R2 media missing for pending appids: {missing}")
    return out


def _summary(manifest, appid):
    """Compact manifest line the model reads alongside the sprite sheet."""
    return (f"appid {appid}: {manifest.get('name', '')} | {manifest.get('meta', '')} | "
            f"{(manifest.get('desc') or '')[:200]}")


def _sheet_b64(folder, manifest):
    name = next((f for f in manifest.get("images", [])
                 if "SpriteSheet" in f and f.lower().endswith(".png")), None)
    if not name:
        raise RuntimeError(f"sprite sheet missing from R2 manifest: {folder}")
    req = urllib.request.Request(media_store.public_url(folder, name), headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as response:
        return base64.b64encode(response.read()).decode()


RETRIES = 10         # patient: a free key gets rate-limited (429) / slow / flaky
TIMEOUT = 120        # per request; free tier can be slow under load


def _backoff(e, attempt):
    """How long to wait before the next retry. Honor a Retry-After header if the API
    sends one; on a 429 wait out the per-minute window (free-tier RPM resets each
    minute); otherwise exponential backoff with jitter."""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            return float(resp.headers.get("retry-after")) + random.uniform(0, 3)
        except (TypeError, ValueError, AttributeError):
            pass
    if getattr(e, "status_code", None) == 429:
        return 62 + random.uniform(0, 10)            # let the RPM window reset
    return min(120, 2 ** attempt + random.uniform(0, 3))


def _ask(client, model, batch):
    """One vision call over a batch; return rejected appids (set).
    Retries with exponential backoff + jitter on any API error (rate-limit, timeout,
    5xx). Raises after RETRIES; the caller fails the workflow so it can be rerun."""
    ids = [appid for appid, _, _ in batch]
    content = [{"type": "text", "text": RUBRIC.format(ids=ids)}]
    for appid, folder, manifest in batch:
        content.append({"type": "text", "text": _summary(manifest, appid)})
        b64 = _sheet_b64(folder, manifest)
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    msgs = [{"role": "user", "content": content}]
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=400, timeout=TIMEOUT, messages=msgs)
            tail = (resp.choices[0].message.content or "").split("Rejected Games")[-1]
            return {int(n) for n in re.findall(r"\d+", tail)}
        except Exception as e:
            if attempt == RETRIES - 1:
                raise
            wait = _backoff(e, attempt)
            print(f"    api error ({type(e).__name__}), retry {attempt + 1}/{RETRIES} "
                  f"in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)


def main():
    base = os.environ.get("TRIAGE_BASE_URL")
    key = os.environ.get("TRIAGE_API_KEY")
    model = os.environ.get("TRIAGE_MODEL")
    if not (base and key and model):
        sys.exit("set TRIAGE_BASE_URL / TRIAGE_API_KEY / TRIAGE_MODEL")
    if not media_store.read_enabled() or not media_store.write_enabled():
        sys.exit("R2 read/write configuration is required")
    from openai import OpenAI
    client = OpenAI(base_url=base, api_key=key, timeout=TIMEOUT, max_retries=0)  # we retry

    try:
        games = _games()
    except RuntimeError as e:
        print(f"triage input failed: {e}", file=sys.stderr)
        return 1
    if not games:
        print("no Pending leads to triage")
        return 0

    rejected, skipped = [], 0
    for i in range(0, len(games), BATCH):
        batch = games[i:i + BATCH]
        ids = {appid for appid, _, _ in batch}
        print(f"  triaging {i + 1}-{i + len(batch)} of {len(games)}...", file=sys.stderr)
        if i:
            time.sleep(3)            # polite spacing to stay under the free-tier RPM
        try:
            rejected += sorted(_ask(client, model, batch) & ids)   # keep only real batch ids
        except Exception as e:
            # Keep the queue intact so a manual rerun retries the complete batch.
            skipped += len(batch)
            print(f"  batch left for RETRY after {RETRIES} attempts: "
                  f"{type(e).__name__}", file=sys.stderr)
    rejected = sorted(set(rejected))

    print(f"Rejected Games = {rejected}")
    if skipped:
        print(f"NOTE: {skipped} game(s) couldn't be triaged; rerun Triage.", file=sys.stderr)
    existing = {int(appid) for appid in media_store.fetch_irrelevant()}
    merged = sorted(existing | set(rejected))
    media_store.write_irrelevant(merged)
    print(f"merged {len(rejected)} new flag(s) -> {len(merged)} in R2 irrelevant.json")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
