"""Cloud triage: a VISION model reviews each staged game's sprite sheet + JSON and
flags irrelevant appids into R2's irrelevant.json. The app then gates a reject-review
on that list. Runs as the last step of the GHA discovery job (folders still on the
runner). NON-destructive — it only flags; the human Accepts/Rejects in the app.

Provider-agnostic (OpenAI-compatible). Set:
    TRIAGE_BASE_URL   e.g. https://api.moonshot.cn/v1  (must be a VISION endpoint)
    TRIAGE_MODEL      a vision-capable model id
    TRIAGE_API_KEY
Plus the R2 write creds (so it can write irrelevant.json). See media_store.py.
"""
import base64
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "Leads_Reviewer"))
import media_store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REVIEW_DIR = os.path.join(os.path.dirname(HERE), "Leads_Reviewer", "Studios_To_Review")
SUBDIRS = ("Approval_Pending_Games", "No_Mail_Games")
BATCH = 4                         # games (= images) per call; keeps appid<->image mapping clean

RUBRIC = """You are a Games Reviewer/filterer for a freelancer who pitches game-programming
to studios that would benefit from hiring a developer. For EACH game below, use its JSON and
— most importantly — its sprite-sheet image (the sheet is the best read on the game; analyse
it per game). REJECT a game if it is any of: a desktop app/utility; Dating Sim / Romance;
Visual Novel / Interactive Fiction; a 2D game; something a single developer could clearly
build alone; or AI slop. ALLOW everything else. Output ONE line, nothing else, listing the
rejected appids from THIS set {ids}:
Rejected Games = [4858620, 4819850]"""


def _games():
    """(appid, folder_path) for every staged game folder ending in _<appid>."""
    out = []
    for sub in SUBDIRS:
        base = os.path.join(REVIEW_DIR, sub)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            m = re.search(r"_(\d+)$", name)
            p = os.path.join(base, name)
            if m and os.path.isdir(p):
                out.append((int(m.group(1)), p))
    return out


def _summary(folder, appid):
    """Compact JSON line for one game (name/genres/desc) the model reads alongside the image."""
    try:
        data = json.load(open(os.path.join(folder, f"{appid}.json"), encoding="utf-8"))
    except Exception:
        return f"appid {appid}: (no json)"
    genres = ", ".join(g.get("description", "") for g in (data.get("genres") or [])
                       if isinstance(g, dict))
    return (f"appid {appid}: {data.get('gameName', '')} | genres: {genres} | "
            f"{(data.get('short_description') or '')[:200]}")


def _sheet(folder):
    for f in os.listdir(folder):
        if "SpriteSheet" in f and f.lower().endswith(".png"):
            return os.path.join(folder, f)
    return None


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
    """One vision call over `batch` ([(appid, folder)]); return rejected appids (set).
    Retries with exponential backoff + jitter on any API error (rate-limit, timeout,
    5xx). Raises only after RETRIES exhausted — the caller fail-opens that batch."""
    ids = [a for a, _ in batch]
    content = [{"type": "text", "text": RUBRIC.format(ids=ids)}]
    for appid, folder in batch:
        content.append({"type": "text", "text": _summary(folder, appid)})
        sheet = _sheet(folder)
        if sheet:
            b64 = base64.b64encode(open(sheet, "rb").read()).decode()
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
    from openai import OpenAI
    client = OpenAI(base_url=base, api_key=key, timeout=TIMEOUT, max_retries=0)  # we retry

    games = _games()
    if not games:
        print("no staged games — nothing to triage")
        if media_store.write_enabled():
            media_store.write_irrelevant([])
        return 0

    rejected, skipped = [], 0
    for i in range(0, len(games), BATCH):
        batch = games[i:i + BATCH]
        ids = {a for a, _ in batch}
        print(f"  triaging {i + 1}-{i + len(batch)} of {len(games)}...", file=sys.stderr)
        if i:
            time.sleep(3)            # polite spacing to stay under the free-tier RPM
        try:
            rejected += sorted(_ask(client, model, batch) & ids)   # keep only real batch ids
        except Exception as e:
            # exhausted retries — fail OPEN: leave these as ALLOWED so they fall through to
            # your normal review (never wrongly drop a lead because the API was down).
            skipped += len(batch)
            print(f"  batch left for MANUAL review after {RETRIES} retries: "
                  f"{type(e).__name__}", file=sys.stderr)
    rejected = sorted(set(rejected))

    print(f"Rejected Games = {rejected}")
    if skipped:
        print(f"NOTE: {skipped} game(s) couldn't be triaged (API) — left ALLOWED "
              f"for manual review.")
    if media_store.write_enabled():
        media_store.write_irrelevant(rejected)
        print(f"wrote {len(rejected)} flagged appid(s) -> R2 irrelevant.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
