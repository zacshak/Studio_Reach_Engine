"""Leads Reviewer — review staged game leads as individual containers.

Renders one card per game that is still awaiting review (Mail_status == 'Pending'
and has a media folder). Each card shows the game's screenshots and short
description with two actions:

  • Allow  -> Mail_status becomes 'Writing'; the card leaves the view.
  • Reject -> the lead row and its media folder are deleted; the card disappears.

All data + actions go through Reviewer_Interface (never pipeline.py directly).

    python reviewer.py
"""
import re
import sys
import tkinter as tk
from tkinter import font as tkfont, messagebox

from PIL import Image, ImageTk

import Reviewer_Interface as review

# --- palette ---------------------------------------------------------------
BG       = "#161a22"
CARD     = "#1f2530"
RAIL     = "#11151c"
TEXT     = "#e7eaf0"
MUTED    = "#8b93a7"
ACCENT   = "#4f8cff"
REJECT   = "#e5484d"
REJECT_H = "#f0666a"
APPROVE  = "#2ea043"
APPROVE_H= "#3fb955"

THUMB_H   = 104    # screenshot thumbnail height in a card
MAX_THUMBS = 4     # cap thumbnails per card (bounds memory; rest shown as "+N")
LAZY_MARGIN = 400  # px above/below viewport to pre-load thumbnails


class Reviewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Leads Reviewer")
        self.configure(bg=BG)
        self.geometry("1080x780")
        self.minsize(900, 600)

        self._fonts()
        self.leads = review.games_to_review()
        self.cards = {}            # appid -> card frame
        self._info = {}            # appid -> {card, strip, shots, loaded}
        self._desc_labels = []     # for dynamic re-wrapping
        self._building = True       # suppress scrollregion churn during bulk build
        self._lazy_pending = False
        self._resize_after = None
        self._width = 1040

        self._build_header()
        self._build_scroll()
        self._populate()                  # builds cards incrementally (non-blocking)

    # -- fonts / buttons ----------------------------------------------------
    def _fonts(self):
        self.f_brand = tkfont.Font(family="Segoe UI Semibold", size=14)
        self.f_title = tkfont.Font(family="Segoe UI Semibold", size=17)
        self.f_meta  = tkfont.Font(family="Segoe UI", size=10)
        self.f_body  = tkfont.Font(family="Segoe UI", size=11)
        self.f_small = tkfont.Font(family="Segoe UI", size=9)
        self.f_btn   = tkfont.Font(family="Segoe UI Semibold", size=10)

    def _btn(self, parent, text, base, hover, cmd):
        b = tk.Button(parent, text=text, command=cmd, font=self.f_btn,
                      bg=base, fg="#ffffff", activebackground=hover,
                      activeforeground="#ffffff", relief="flat", bd=0,
                      cursor="hand2", width=10, pady=7)
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=base))
        return b

    # -- layout -------------------------------------------------------------
    def _build_header(self):
        bar = tk.Frame(self, bg=RAIL, height=56)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Label(bar, text="◆  Leads Reviewer", bg=RAIL, fg=TEXT,
                 font=self.f_brand).pack(side="left", padx=20)
        self.counter = tk.Label(bar, text="", bg=RAIL, fg=MUTED, font=self.f_meta)
        self.counter.pack(side="right", padx=20)

    def _build_scroll(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vs = tk.Scrollbar(wrap, orient="vertical", command=self._on_scroll)
        self.canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.holder = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.holder, anchor="nw")
        self.holder.bind("<Configure>", self._on_holder_configure)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_holder_configure(self, _e):
        if not self._building:                # skip the O(n^2) churn during bulk build
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_scroll(self, *args):
        self.canvas.yview(*args)
        self._schedule_lazy()

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")
        self._schedule_lazy()

    def _on_canvas_resize(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)   # cards fill width
        self._width = e.width
        if self._resize_after:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(120, self._apply_wrap)   # debounce rewrap

    def _apply_wrap(self):
        wl = max(420, self._width - 120)
        for lbl in self._desc_labels:
            lbl.config(wraplength=wl)
        self._remeasure()                     # heights changed -> refresh geometry

    def _remeasure(self):
        """Refresh layout once (after build / resize / removal), then lazy-load.
        The only place we force a relayout — kept off the scroll path."""
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._lazy_load()

    def _schedule_lazy(self):
        if not self._lazy_pending:            # coalesce rapid scroll events
            self._lazy_pending = True
            self.after_idle(self._lazy_load)

    def _lazy_load(self):
        """Decode + show thumbnails only for cards near the viewport. Reads cached
        widget geometry (no forced relayout — that's what made scrolling lag)."""
        self._lazy_pending = False
        top = self.canvas.canvasy(0) - LAZY_MARGIN
        bot = self.canvas.canvasy(self.canvas.winfo_height()) + LAZY_MARGIN
        for info in self._info.values():
            if info["loaded"]:
                continue
            card = info["card"]
            y = card.winfo_y()
            if y + card.winfo_height() >= top and y <= bot:
                self._load_thumbs(info)

    def _load_thumbs(self, info):
        info["loaded"] = True
        strip = info["strip"]
        shots = info["shots"][:MAX_THUMBS]
        for path in shots:
            try:
                im = Image.open(path)
                im.draft("RGB", (THUMB_H * 2, THUMB_H))   # fast partial JPEG decode
                w = int(im.width * THUMB_H / im.height)
                tkim = ImageTk.PhotoImage(im.resize((w, THUMB_H), Image.BILINEAR))
            except Exception:
                continue
            lbl = tk.Label(strip, image=tkim, bg=CARD, bd=0, cursor="hand2")
            lbl.image = tkim
            lbl.bind("<Button-1>", lambda e, p=path: self._open_image(p))
            lbl.pack(side="left", padx=(0, 6))
        extra = len(info["shots"]) - len(shots)
        if extra > 0:
            tk.Label(strip, text=f"+{extra}", bg=CARD, fg=MUTED, font=self.f_body,
                     width=5).pack(side="left", padx=6)
        if not info["shots"]:
            tk.Label(strip, text="no screenshots", bg=CARD, fg=MUTED,
                     font=self.f_small).pack(side="left", padx=4)

    # -- cards --------------------------------------------------------------
    def _populate(self):
        """Build cards in small chunks via the event loop so the window appears
        immediately and fills in, instead of freezing until all are laid out."""
        if not self.leads:
            self._building = False
            self._update_counter()
            self._empty()
            return
        self._queue = list(self.leads)
        self._build_next()

    def _build_next(self):
        for _ in range(6):
            if not self._queue:
                break
            self._make_card(self._queue.pop(0))
        self._update_counter()
        if self._queue:
            self.after(1, self._build_next)
        else:
            self._building = False           # done -> enable scrollregion + measure
            self.after(20, self._remeasure)

    def _make_card(self, lead):
        appid = lead["appid"]
        card = tk.Frame(self.holder, bg=CARD)
        card.pack(fill="x", padx=18, pady=9)

        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(top, text=lead["name"], bg=CARD, fg=TEXT, font=self.f_title,
                 anchor="w").pack(side="left")
        actions = tk.Frame(top, bg=CARD)
        actions.pack(side="right")
        self._btn(actions, "✗  Reject", REJECT, REJECT_H,
                  lambda: self._reject(appid)).pack(side="right", padx=(8, 0))
        self._btn(actions, "✓  Allow", APPROVE, APPROVE_H,
                  lambda: self._accept(appid)).pack(side="right")

        tk.Label(card, text=lead["meta"], bg=CARD, fg=ACCENT, font=self.f_meta,
                 anchor="w").pack(fill="x", padx=20, pady=(0, 10))

        # fixed-height strip; thumbnails are filled in lazily (_load_thumbs) so the
        # card height never changes -> the scroll position can't jump as you scroll
        strip = tk.Frame(card, bg=CARD, height=THUMB_H)
        strip.pack(fill="x", padx=20)
        strip.pack_propagate(False)

        desc = tk.Label(card, text=lead["desc"], bg=CARD, fg=TEXT, font=self.f_body,
                        anchor="nw", justify="left", wraplength=940)
        desc.pack(fill="x", padx=20, pady=(12, 18))
        self._desc_labels.append(desc)

        self.cards[appid] = card
        self._info[appid] = {"card": card, "strip": strip,
                             "shots": lead["shots"], "loaded": False}

    # -- lightbox -----------------------------------------------------------
    def _open_image(self, path):
        """Show one screenshot full-screen, scaled to fit. Click or Esc closes."""
        win = tk.Toplevel(self)
        win.configure(bg="black")
        win.attributes("-fullscreen", True)
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Button-1>", lambda e: win.destroy())
        try:
            im = Image.open(path)
        except Exception:
            win.destroy()
            return
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        scale = min(sw / im.width, sh / im.height)
        size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        tkim = ImageTk.PhotoImage(im.resize(size, Image.LANCZOS))
        lbl = tk.Label(win, image=tkim, bg="black", cursor="hand2")
        lbl.image = tkim
        lbl.bind("<Button-1>", lambda e: win.destroy())
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(win, text="click or Esc to close", bg="black", fg="#6b7280",
                 font=self.f_small).place(relx=0.5, rely=0.985, anchor="s")

    # -- actions ------------------------------------------------------------
    def _accept(self, appid):
        try:
            review.Accept_Game(appid)
        except Exception as e:
            messagebox.showerror("Allow failed", str(e))
            return
        self._remove(appid)

    def _reject(self, appid):
        name = next((l["name"] for l in self.leads if l["appid"] == appid), appid)
        if not messagebox.askyesno(
                "Reject lead",
                f"Reject “{name}”?\n\nThis permanently deletes its row and its "
                f"screenshot folder."):
            return
        try:
            review.Reject_Game(appid)
        except Exception as e:
            messagebox.showerror("Reject failed", str(e))
            return
        self._remove(appid)

    def _remove(self, appid):
        card = self.cards.pop(appid, None)
        self._info.pop(appid, None)
        if card:
            card.destroy()
        self.leads = [l for l in self.leads if l["appid"] != appid]
        self._update_counter()
        if not self.cards:
            self._empty()
        self.after_idle(self._remeasure)      # remaining cards shifted up

    def _update_counter(self):
        self.counter.config(text=f"{len(self.cards)} to review")

    def _empty(self):
        if getattr(self, "_empty_shown", False):
            return
        self._empty_shown = True
        tk.Label(self.holder, text="All caught up — nothing left to review.",
                 bg=BG, fg=MUTED, font=self.f_title).pack(pady=80)


def _cli_delete(argv):
    """Bulk-reject appids from the command line, no GUI. Accepts any format:
    `-delete 1 2 3`, `-delete [1, 2, 3]`, etc. (any non-digits are separators)."""
    appids = [int(n) for n in re.findall(r"\d+", " ".join(argv))]
    if not appids:
        sys.exit("no appids given. e.g. python reviewer.py -delete [4858620, 4819850]")
    print(f"rejecting {len(appids)} lead(s)...")
    done = 0
    for appid in appids:
        try:
            review.Reject_Game(appid)
            done += 1
            print(f"  deleted {appid}")
        except Exception as e:
            print(f"  FAILED {appid}: {e}")
    print(f"done: {done}/{len(appids)} rejected (row + media folder removed)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-delete", "--delete"):
        _cli_delete(sys.argv[2:])
    else:
        Reviewer().mainloop()
