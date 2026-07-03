"""Leads Reviewer — review staged game leads as individual containers.

Renders one card per game that is still awaiting review (Mail_status == 'Pending',
has an email on file, and has a media folder). Each card shows the game's screenshots and short
description with two actions:

  • Allow  -> Mail_status becomes 'Writing'; the card leaves the view.
  • Reject -> the lead row and its media folder are deleted; the card disappears.

All data + actions go through Reviewer_Interface (never pipeline.py directly).

    python reviewer.py
"""
import json
import os
import re
import sys
import tkinter as tk
import webbrowser
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
MAIL_ROWS = 18     # visible rows of the mail text field (scrolls internally beyond)
MAIL_DESC_WRAP = 760  # desc wrap width in the mail card's left column

# toggle sections: (mode key, label, highlight colour)
MODES = (("game", "Game Approval", "#4f8cff"),
         ("mail", "Mail Approval", "#6b46c1"),
         ("nomail", "NoMail Games", "#b45309"))


class Reviewer(tk.Tk):
    def __init__(self, delete_appids=None):
        super().__init__()
        self.delete_ids = delete_appids
        # "delete" = review-before-delete window (from `--delete`); no toggle,
        # one Delete button per card. Otherwise the normal 3-mode reviewer.
        self.mode = "delete" if delete_appids else "game"
        self.title("Leads Reviewer — Delete Review" if delete_appids
                   else "Leads Reviewer")
        self.configure(bg=BG)
        self.geometry("1080x780")
        self.minsize(900, 600)

        self._fonts()
        self.leads = self._load_source()
        self.cards = {}            # appid -> card frame
        self._info = {}            # appid -> {card, strip, shots, loaded}
        self._desc_labels = []     # for dynamic re-wrapping
        self._mail_texts = []      # mail Text widgets, auto-sized to their content
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
        self.f_title_u = tkfont.Font(family="Segoe UI Semibold", size=17, underline=True)
        self.f_meta  = tkfont.Font(family="Segoe UI", size=10)
        self.f_body  = tkfont.Font(family="Segoe UI", size=11)
        self.f_small = tkfont.Font(family="Segoe UI", size=9)
        self.f_btn   = tkfont.Font(family="Segoe UI Semibold", size=10)

    def _steam_link(self, label, appid):
        """Turn a game-name label into a clickable link to its Steam store page."""
        url = f"https://store.steampowered.com/app/{appid}/"
        label.config(fg=ACCENT, cursor="hand2")
        label.bind("<Button-1>", lambda e: webbrowser.open(url))
        label.bind("<Enter>", lambda e: label.config(font=self.f_title_u))
        label.bind("<Leave>", lambda e: label.config(font=self.f_title))

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
        if self.mode == "delete":                 # no toggle in the delete window
            tk.Label(bar, text="🗑  Delete Review — confirm each before removing",
                     bg=RAIL, fg=REJECT_H, font=self.f_brand).pack(side="left", padx=20)
        else:
            self._tg_w, self._tg_h = 430, 36
            self.toggle = tk.Canvas(bar, width=self._tg_w, height=self._tg_h, bg=RAIL,
                                    highlightthickness=0, cursor="hand2")
            self.toggle.pack(side="left", padx=16, pady=10)
            self.toggle.bind("<Button-1>", self._toggle_click)
            self._style_toggle()
            tk.Label(bar, text="◆  Leads Reviewer", bg=RAIL, fg=TEXT,
                     font=self.f_brand).pack(side="left", padx=8)
        self.counter = tk.Label(bar, text="", bg=RAIL, fg=MUTED, font=self.f_meta)
        self.counter.pack(side="right", padx=20)

    def _round_rect(self, c, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    def _style_toggle(self):
        """Segmented pill: each section is a clickable third; the active one is lit."""
        c, W, H = self.toggle, self._tg_w, self._tg_h
        seg = W / len(MODES)
        c.delete("all")
        self._round_rect(c, 1, 1, W - 1, H - 1, 17, fill=CARD, outline="")
        for i, (key, label, hl) in enumerate(MODES):
            x1, x2 = i * seg, (i + 1) * seg
            if key == self.mode:
                self._round_rect(c, x1 + 2, 2, x2 - 2, H - 2, 16, fill=hl, outline="")
            fg = "#ffffff" if key == self.mode else MUTED
            c.create_text((x1 + x2) / 2, H / 2, text=label, fill=fg, font=self.f_btn)

    def _toggle_click(self, e):
        idx = min(len(MODES) - 1, max(0, int(e.x / (self._tg_w / len(MODES)))))
        key = MODES[idx][0]
        if key != self.mode:
            self.mode = key
            self._style_toggle()
            self._reload()

    def _load_source(self):
        if self.mode == "delete":
            return review.leads_by_appids(self.delete_ids)
        if self.mode == "mail":
            return review.mails_to_review()
        if self.mode == "nomail":
            return review.nomail_games_to_review()
        return review.games_to_review()

    def _reload(self):
        """Switch sections: tear down current cards, repopulate from the new source."""
        for child in self.holder.winfo_children():   # cards + any "empty" label
            child.destroy()
        self.cards.clear()
        self._info.clear()
        self._desc_labels.clear()
        self._mail_texts.clear()
        self._empty_shown = False
        self.canvas.yview_moveto(0)
        self._building = True
        self.leads = self._load_source()
        self._populate()

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
        # mail cards keep the desc in a fixed-width left column; only game cards rewrap
        wl = MAIL_DESC_WRAP if self.mode == "mail" else max(420, self._width - 120)
        for lbl in self._desc_labels:
            lbl.config(wraplength=wl)
        self._fit_mail_heights()              # wrap width changed -> re-fit mail boxes
        self._remeasure()                     # heights changed -> refresh geometry

    def _fit_mail_heights(self):
        """Size each mail Text to show every line — no internal scrolling. The line
        count depends on the current wrap width, so this re-runs on resize too."""
        if not self._mail_texts:
            return
        self.update_idletasks()               # let the text widths settle first
        for t in self._mail_texts:
            try:
                res = t.count("1.0", "end-1c", "displaylines")
            except tk.TclError:
                continue
            n = res[0] if isinstance(res, (tuple, list)) else int(res or 0)
            t.config(height=max(3, n + 1))    # +1 keeps the last line off the edge

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
            self.after(20, self._fit_mail_heights)
            self.after(40, self._remeasure)

    def _make_card(self, lead):
        if self.mode == "mail":
            self._make_mail_card(lead)
        else:
            self._make_game_card(lead, allow=self.mode == "game")

    def _make_game_card(self, lead, allow=True):
        appid = lead["appid"]
        card = tk.Frame(self.holder, bg=CARD)
        card.pack(fill="x", padx=18, pady=9)

        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=20, pady=(16, 2))
        name = tk.Label(top, text=lead["name"], bg=CARD, fg=TEXT, font=self.f_title,
                        anchor="w")
        name.pack(side="left")
        self._steam_link(name, appid)
        actions = tk.Frame(top, bg=CARD)
        actions.pack(side="right")
        rej_text = "🗑  Delete" if self.mode == "delete" else "✗  Reject"
        self._btn(actions, rej_text, REJECT, REJECT_H,
                  lambda: self._reject(appid)).pack(side="right", padx=(8, 0))
        if allow:                                    # NoMail / delete are reject-only
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

    def _make_mail_card(self, lead):
        """Mail Approval card: left = screenshots + desc, right = drafted mail in a
        big text field with its own Accept (-> Scheduled) / Reject below."""
        appid = lead["appid"]
        card = tk.Frame(self.holder, bg=CARD)
        card.pack(fill="x", padx=18, pady=9)

        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=20, pady=(16, 2))
        name = tk.Label(head, text=lead["name"], bg=CARD, fg=TEXT, font=self.f_title,
                        anchor="w")
        name.pack(side="left")
        self._steam_link(name, appid)
        tk.Label(head, text=lead.get("emails") or "(no email)", bg=CARD,
                 fg=APPROVE_H if lead.get("emails") else MUTED, font=self.f_body,
                 anchor="w").pack(side="left", padx=(14, 0), pady=(6, 0))
        tk.Label(card, text=lead["meta"], bg=CARD, fg=ACCENT, font=self.f_meta,
                 anchor="w").pack(fill="x", padx=20, pady=(0, 10))

        split = tk.Frame(card, bg=CARD)
        split.pack(fill="x", padx=20, pady=(0, 18))

        # left = screenshots + desc (natural width); right = mail, expands to fill
        # all remaining width so the field is big, not crammed into a corner
        left = tk.Frame(split, bg=CARD)
        left.pack(side="left", fill="y")
        strip = tk.Frame(left, bg=CARD, height=THUMB_H)
        strip.pack(fill="x")
        strip.pack_propagate(False)
        desc = tk.Label(left, text=lead["desc"], bg=CARD, fg=TEXT, font=self.f_body,
                        anchor="nw", justify="left", wraplength=MAIL_DESC_WRAP)
        desc.pack(fill="x", pady=(12, 0))
        self._desc_labels.append(desc)

        right = tk.Frame(split, bg=CARD)
        right.pack(side="right", fill="both", expand=True, padx=(18, 0))
        # buttons packed FIRST at the bottom so they're never clipped
        macts = tk.Frame(right, bg=CARD)
        macts.pack(side="bottom", fill="x", pady=(10, 0))
        txt = tk.Text(right, height=3, bg=RAIL, fg=TEXT, bd=0, relief="flat",
                      font=self.f_body, wrap="word", padx=14, pady=12,
                      highlightthickness=0)
        txt.pack(side="top", fill="both", expand=True)
        txt.insert("1.0", lead.get("mail") or "(no mail draft found)")
        txt.config(state="disabled")                  # read-only: no editing
        self._mail_texts.append(txt)                  # height fitted after layout
        self._btn(macts, "✗  Reject", REJECT, REJECT_H,
                  lambda: self._reject(appid)).pack(side="right", padx=(8, 0))
        self._btn(macts, "✓  Accept", APPROVE, APPROVE_H,
                  lambda: self._accept_mail(appid)).pack(side="right")

        self.cards[appid] = card
        self._info[appid] = {"card": card, "strip": strip,
                             "shots": lead["shots"], "loaded": False}

    # -- lightbox -----------------------------------------------------------
    def _open_image(self, path):
        """Full-screen viewer. ← / → step through EVERY photo across all games (in
        display order, crossing game boundaries and wrapping). Click or Esc closes."""
        flat = [(p, lead["name"]) for lead in self.leads for p in lead["shots"]]
        paths = [p for p, _ in flat]
        idx = paths.index(path) if path in paths else 0
        if not flat:
            flat, idx = [(path, "")], 0

        win = tk.Toplevel(self)
        win.configure(bg="black")
        win.attributes("-fullscreen", True)
        lbl = tk.Label(win, bg="black", cursor="hand2")
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        lbl.bind("<Button-1>", lambda e: win.destroy())
        title = tk.Label(win, bg="black", fg=TEXT, font=self.f_title)
        title.place(x=24, y=18, anchor="nw")
        hint = tk.Label(win, bg="black", fg="#6b7280", font=self.f_small)
        hint.place(relx=0.5, rely=0.985, anchor="s")

        self._lb = {"win": win, "list": flat, "idx": idx, "label": lbl,
                    "hint": hint, "title": title}
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Left>", lambda e: self._lb_step(-1))
        win.bind("<Right>", lambda e: self._lb_step(1))
        win.focus_force()                         # so the arrow keys land here
        self._lb_render()

    def _lb_step(self, d):
        lb = self._lb
        new = lb["idx"] + d
        if 0 <= new < len(lb["list"]):            # clamp at the ends — no wrap-around
            lb["idx"] = new
            self._lb_render()

    def _lb_render(self):
        lb = self._lb
        path, name = lb["list"][lb["idx"]]
        try:
            im = Image.open(path)
        except Exception:
            return
        sw, sh = lb["win"].winfo_screenwidth(), lb["win"].winfo_screenheight()
        scale = min(sw / im.width, sh / im.height)
        size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        tkim = ImageTk.PhotoImage(im.resize(size, Image.LANCZOS))
        lb["label"].configure(image=tkim)
        lb["label"].image = tkim                  # keep a ref so it isn't GC'd
        lb["title"].config(text=name)
        lb["hint"].config(
            text=f"{lb['idx'] + 1} / {len(lb['list'])}     ←  →  ·  Esc to close")

    # -- actions ------------------------------------------------------------
    def _accept(self, appid):
        try:
            review.Accept_Game(appid)
        except Exception as e:
            messagebox.showerror("Allow failed", str(e))
            return
        self._remove(appid)

    def _accept_mail(self, appid):
        try:
            review.Approve_Mail(appid)
        except Exception as e:
            messagebox.showerror("Accept failed", str(e))
            return
        self._remove(appid)

    def _reject(self, appid):
        verb = "Delete" if self.mode == "delete" else "Reject"
        name = next((l["name"] for l in self.leads if l["appid"] == appid), appid)
        if not messagebox.askyesno(
                f"{verb} lead",
                f"{verb} “{name}”?\n\nThis permanently deletes its row from both "
                f"tables and its screenshot folder on disk."):
            return
        try:
            leftover = review.Reject_Game(appid)
        except Exception as e:
            messagebox.showerror(f"{verb} failed", str(e))
            return
        if leftover:                              # folder locked — rows gone, dir stayed
            messagebox.showwarning(
                f"{verb}d — folder left behind",
                f"Removed the DB rows for “{name}”, but its folder is still on disk "
                f"(likely open/locked):\n\n" + "\n".join(leftover))
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
        noun = "to delete" if self.mode == "delete" else "to review"
        self.counter.config(text=f"{len(self.cards)} {noun}")

    def _empty(self):
        if getattr(self, "_empty_shown", False):
            return
        self._empty_shown = True
        tk.Label(self.holder, text="All caught up — nothing left to review.",
                 bg=BG, fg=MUTED, font=self.f_title).pack(pady=80)


def _parse_items(text):
    """Lenient JSON: tolerate a bare object list with no [] and trailing commas
    (so the scraper output can be pasted verbatim)."""
    text = text.strip()
    if not text.startswith("["):
        text = "[" + text + "]"
    text = re.sub(r",\s*([}\]])", r"\1", text)     # drop trailing commas
    return json.loads(text)


def _cli_ingest(argv):
    """--ingest-mailids: read scraped {url,email} objects (from a file arg, the
    remaining args, or stdin), apply the ones with a real email."""
    if argv and os.path.isfile(argv[0]):
        text = open(argv[0], encoding="utf-8").read()
    else:
        text = " ".join(argv) if argv else sys.stdin.read()
    res = review.ingest_emails(_parse_items(text))
    print(f"updated {len(res['updated'])}, skipped (no email) {len(res['skipped'])}, "
          f"unmatched {len(res['unmatched'])}")
    for a, e in res["updated"]:
        print(f"  scraped {a} <- {e}  (folder -> Approval_Pending_Games)")
    if res["unmatched"]:
        print("  unmatched urls:", res["unmatched"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-ingest-mailids", "--ingest-mailids"):
        _cli_ingest(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] in ("-pending-urls", "--pending-urls",
                                               "-pending", "--pending"):
        # print the pending leads' website URLs as a brace-wrapped, quoted list
        urls = review.pending_website_urls()
        print("{" + ", ".join(f'"{u}"' for u in urls) + "}")
    elif len(sys.argv) > 1 and sys.argv[1] in ("-delete", "--delete"):
        # Review-before-delete: open a GUI of just these appids, each with a single
        # Delete button (same effect as Reject). Any format works — `--delete 1 2 3`,
        # `--delete [1, 2, 3]`, etc. (any non-digit is a separator).
        appids = [int(n) for n in re.findall(r"\d+", " ".join(sys.argv[2:]))]
        if not appids:
            sys.exit("no appids given. e.g. python reviewer.py --delete [4858620, 4819850]")
        Reviewer(delete_appids=appids).mainloop()
    else:
        Reviewer().mainloop()
