"""SRE --irrelevants-list: a headless Claude Code agent vision-reviews every staged
game (its 2x2 spritesheet + JSON) and returns the appids worth rejecting.

Runs `claude -p` read-only, in BATCHES of game folders (the queue can reach ~100 games,
and vision quality + context degrade when one call holds that many ~1MP spritesheets),
then merges the rejected appids. Prints:

    Rejected Games = [4858620, 4819850, ...]

Feed that straight into:  python SRE.py --delete [...]   (GUI review-before-delete).

Per batch we keep only the appids the agent returns that are ACTUALLY in that batch, so a
hallucinated or stray number can't sneak into the delete list.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW_DIR = os.path.join(HERE, "Studios_To_Review")
SUBDIRS = ("Approval_Pending_Games", "No_Mail_Games")
BATCH = 12                      # game folders per claude call (each has one ~1MP sheet)
MODEL = "sonnet"                # triage is a cheap filter; no need for opus
CLAUDE = "claude.cmd" if os.name == "nt" else "claude"   # npm .ps1 shim isn't exec-able

RUBRIC = """You are a Games Reviewer/filterer. I pitch freelance/contract game-programming
to studios that would benefit from hiring a developer. Review each game below using its
JSON and — most importantly — its SpriteSheet_Screenshots.png (the spritesheet gives the
best read on the game; analysing it per game is a MUST).

REJECT a game if it is any of: a desktop app/utility; Dating Sim / Romance; Visual Novel /
Interactive Fiction; a 2D game; something a single developer could clearly build alone; or
AI slop. ALLOW everything else (games that might benefit from hiring a developer).

Review ONLY these game folders (relative to the current directory):
{folders}

Read each folder's SpriteSheet_Screenshots.png and its *.json. Then output ONE line, nothing
else, listing the rejected games' appids exactly in this format:
Rejected Games = [4858620, 4819850]"""


def _game_folders():
    """(appid, 'Subdir/Folder_Name') for every staged game folder ending in _<appid>."""
    out = []
    for sub in SUBDIRS:
        base = os.path.join(REVIEW_DIR, sub)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            m = re.search(r"_(\d+)$", name)
            if m and os.path.isdir(os.path.join(base, name)):
                out.append((int(m.group(1)), f"{sub}/{name}"))
    return out


def _ask(folders):
    """One read-only claude pass over `folders` (list of 'Subdir/Name'); return the appids
    it rejected as a set of ints (raw — caller intersects with the batch)."""
    prompt = RUBRIC.format(folders="\n".join(folders))
    # prompt goes via stdin, NOT argv: a multi-line arg gets truncated at the first
    # newline by the Windows claude.cmd shim (agent then sees only a fragment).
    r = subprocess.run(
        [CLAUDE, "-p", "--model", MODEL, "--allowedTools", "Read Glob Grep"],
        input=prompt, cwd=REVIEW_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace",   # Windows defaults pipes to cp1252 (cpython #105312)
    )
    if r.returncode != 0:
        print(f"  (claude failed on a batch: {r.stderr.strip()[:200]})", file=sys.stderr)
        return set()
    tail = r.stdout.split("Rejected Games")[-1]      # only digits after the marker
    return {int(n) for n in re.findall(r"\d+", tail)}


def main():
    games = _game_folders()
    if not games:
        print("Rejected Games = []")
        print("(no staged game folders found — run SRE --discover first)", file=sys.stderr)
        return 0
    rejected = []
    for i in range(0, len(games), BATCH):
        batch = games[i:i + BATCH]
        ids = {a for a, _ in batch}
        print(f"  reviewing {i + 1}-{i + len(batch)} of {len(games)}...", file=sys.stderr)
        rejected += sorted(_ask([f for _, f in batch]) & ids)   # keep only real batch ids
    print(f"Rejected Games = {sorted(set(rejected))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
