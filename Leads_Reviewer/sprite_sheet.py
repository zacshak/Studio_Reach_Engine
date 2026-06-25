"""Build one 2x2 sprite sheet (SpriteSheet_Screenshots.png) per game folder so a
vision agent evaluates ONE image per game instead of N screenshots.

Picks 4 screenshots spread evenly across the folder (variety over first-4 near-
dupes), lays them in a 2x2 grid with a thin gutter, NO downscale — the shots are
already well under Claude's 1.15 MP cap, so quality is preserved.

    python sprite_sheet.py                 # all folders in Studios_To_Review/Approval_Pending_Games
    python sprite_sheet.py Studios_To_Review/No_Mail_Games   # a specific staging dir (rel or abs)
    python sprite_sheet.py --force         # rebuild even if a sheet exists
    python sprite_sheet.py --selftest
"""
import glob
import os
import sys

from PIL import Image

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = os.path.join(HERE, "Studios_To_Review", "Approval_Pending_Games")
OUT_NAME = "SpriteSheet_Screenshots.png"
GUTTER = 10                      # px between tiles, so the model doesn't read seams
BG = (255, 0, 255)               # flashy magenta — a loud, unmistakable cell separator


def _pick4(paths):
    """Up to 4 shots spread evenly across the list by POSITION (not content —
    we can't judge variety; positional spread just beats the first-4 hero dupes)."""
    if len(paths) <= 4:
        return paths
    step = len(paths) / 4
    return [paths[int(i * step)] for i in range(4)]


def _grid(n):
    """(cols, rows) sized to the shot count so there are no dead cells:
    1->1x1, 2->2x1, 3->2x2 (one blank), 4->2x2."""
    cols = 1 if n == 1 else 2
    return cols, (n + cols - 1) // cols


def build_sheet(folder, force=False):
    out = os.path.join(folder, OUT_NAME)
    if os.path.exists(out) and not force:
        return False
    shots = sorted(p for p in glob.glob(os.path.join(folder, "*"))
                   if p.lower().endswith((".jpg", ".jpeg", ".png"))
                   and os.path.basename(p) != OUT_NAME)
    if not shots:
        return False                       # 0 screenshots -> no sheet at all
    tiles = [Image.open(p).convert("RGB") for p in _pick4(shots)]
    cw = max(t.width for t in tiles)
    ch = max(t.height for t in tiles)
    # uniform cells so the grid lines up; same-size shots make this a no-op
    tiles = [t if t.size == (cw, ch) else t.resize((cw, ch)) for t in tiles]
    cols, rows = _grid(len(tiles))
    sheet = Image.new("RGB",
                      (cw * cols + GUTTER * (cols - 1),
                       ch * rows + GUTTER * (rows - 1)), BG)
    for i, t in enumerate(tiles):
        c, r = i % cols, i // cols
        sheet.paste(t, (c * (cw + GUTTER), r * (ch + GUTTER)))
    sheet.save(out)
    return True


def main(base, force):
    if not os.path.isdir(base):
        sys.exit(f"no such dir: {base}")
    built = 0
    for entry in sorted(os.listdir(base)):
        folder = os.path.join(base, entry)
        if os.path.isdir(folder) and build_sheet(folder, force):
            built += 1
            print(f"  {entry}/{OUT_NAME}")
    print(f"built {built} sprite sheet(s) in {base}")


def _selftest():
    assert _pick4([1, 2, 3]) == [1, 2, 3]
    assert _pick4(list(range(8))) == [0, 2, 4, 6]
    assert len(_pick4(list(range(100)))) == 4
    assert _grid(1) == (1, 1) and _grid(2) == (2, 1)
    assert _grid(3) == (2, 2) and _grid(4) == (2, 2)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        pos = [a for a in sys.argv[1:] if not a.startswith("--")]
        base = pos[0] if pos else DEFAULT_BASE
        if not os.path.isabs(base):
            base = os.path.join(HERE, base)
        main(base, "--force" in sys.argv)
