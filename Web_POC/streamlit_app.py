"""SRE review UI — the Tkinter reviewer's three approval sections as a phone-friendly
web app. Pure Python: imports Reviewer_Interface and calls the SAME actions the desktop
GUI does. DB is remote Turso (set in .env / Streamlit secrets), so this works anywhere.

    pip install -r requirements.txt
    streamlit run Web_POC/streamlit_app.py

Sections (sidebar):
  Game Approval — Accept (-> 'Writing') / Reject (delete)
  No-Mail       — Reject only (no email to reach them)
  Mail Approval — Approve drafted mail (-> 'Scheduled') / Reject

One card at a time (Tinder-style); acting reloads the shrinking queue. Real actions —
a Reject deletes the lead + its media, same as the desktop GUI.
"""
import os
import sys

import streamlit as st

# On Community Cloud the creds (Turso, R2) arrive via st.secrets, but pipeline.py
# reads os.environ AT IMPORT — so bridge secrets into the env BEFORE importing it.
# Locally there's no secrets.toml, so this is a no-op and .env is used as before.
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except Exception:
    pass

st.set_page_config(page_title="SRE Review", layout="centered")

# Hide Streamlit's own chrome (hamburger menu + "Made with Streamlit" footer) for a
# cleaner review screen. We deliberately KEEP the header element — hiding it would also
# hide the sidebar-expand control on mobile, trapping you in one Section. The Community
# Cloud owner overlay ("Manage app"/Share) is platform chrome shown only to you as
# owner — viewers don't see it and it isn't in this DOM, so it can't be hidden here.
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
[data-testid="stToolbar"] {display: none;}
/* trim the big default top gap so cards sit tight */
.block-container {padding-top: 2rem;}
/* image/swipe iframes must be transparent to touch, else a swipe over the image is
   trapped inside its iframe and never reaches the parent-document swipe handler. We
   don't interact with these iframes directly (display-only), so this is safe. */
iframe {display: block; pointer-events: none;}
</style>
""", unsafe_allow_html=True)

# Fail loud + clear if the DB secret never arrived — otherwise pipeline silently falls
# back to an empty local SQLite and dies cryptically ("no such table: newly_added").
if not os.environ.get("TURSO_DATABASE_URL"):
    st.error(
        "**Turso DB secret missing.** Add `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` "
        "in **Manage app → Settings → Secrets** as flat top-level keys (no `[sections]`), "
        "then **Reboot app**.")
    st.stop()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Leads_Reviewer"))
import Reviewer_Interface as review            # noqa: E402
import streamlit.components.v1 as components    # noqa: E402

SECTION = st.sidebar.radio("Section", ["Game Approval", "No-Mail", "Mail Approval"])

# On phones, hide the Prev/Next buttons — swipe handles navigation. They stay in the
# DOM (display:none, not removed) so the swipe script can still .click() them.
st.markdown(
    "<style>@media (max-width:640px){"
    ".st-key-nav_prev,.st-key-nav_next{display:none!important}}</style>",
    unsafe_allow_html=True)


def _img(url, height=440):
    """Render an image inside a real iframe (components.html) so JS actually runs —
    Streamlit sanitizes st.markdown HTML and strips inline onload, which is why earlier
    CSS/JS spinners never showed. Here a spinner spins until the image's onload fires,
    then the image fades in over it. So a fast swipe shows a spinner, never the previous
    card's screenshot."""
    components.html(f"""
<div style="position:relative;height:{height}px;background:#0e1117;border-radius:8px;
            display:flex;align-items:center;justify-content:center;overflow:hidden;">
  <div style="position:absolute;width:36px;height:36px;border:3px solid rgba(255,255,255,.18);
              border-top-color:rgba(255,255,255,.85);border-radius:50%;
              animation:sresp .8s linear infinite;"></div>
  <img src="{url}" style="position:relative;max-width:100%;max-height:100%;
       object-fit:contain;opacity:0;transition:opacity .25s;"
       onload="this.style.opacity=1;this.previousElementSibling.style.display='none';">
</div>
<style>@keyframes sresp{{to{{transform:rotate(360deg);}}}}
body{{margin:0;}}</style>
""", height=height)


def _card(g, show_mail=False):
    st.subheader(g["name"])
    st.caption(g["meta"])
    st.markdown(f"🔗 [Steam page](https://store.steampowered.com/app/{g['appid']}/)")
    sheet = next((s for s in g["shots"] if "SpriteSheet" in s),
                 g["shots"][0] if g["shots"] else None)
    if sheet:
        _img(sheet)
    if len(g["shots"]) > 1:
        # render-on-demand (not st.expander, which renders its contents even while
        # collapsed → the browser would download all screenshots on every card). The
        # images exist in the DOM only when this is on, so a swipe pulls just the sheet.
        if st.toggle(f"All {len(g['shots'])} screenshots", key=f"all_{g['appid']}"):
            for s in g["shots"]:
                _img(s, height=300)
    st.write(g["desc"])
    if show_mail:
        st.markdown(f"**To:** {g.get('emails') or '—'}")
        st.code(g.get("mail") or "(no draft found)", language=None)


def _reject(appid):
    leftover = review.Reject_Game(appid)              # deletes DB rows + media folder
    return ("couldn't remove: " + ", ".join(leftover)) if leftover else None


# Touch-swipe → click the Prev/Next buttons. Streamlit has no native swipe; this
# binds ONE listener on the parent document (survives reruns; the iframe reloads but
# the parent doc persists) and finds the buttons by label.
# ponytail: clicks buttons by text — fragile if Streamlit restructures the DOM, but it's
# zero-dependency. Buttons below are the real navigation; swipe just drives them.
_SWIPE_JS = """
<script>
const doc = window.parent.document;
if (!doc.__sreSwipe) {
  doc.__sreSwipe = true;
  let x0 = null, y0 = null;
  doc.addEventListener('touchstart', e => {
    x0 = e.changedTouches[0].clientX; y0 = e.changedTouches[0].clientY;
  }, {passive: true});
  doc.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    const dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy)) return;  // ignore taps/scrolls
    const want = dx < 0 ? 'Next' : 'Prev';
    const b = [...doc.querySelectorAll('button')]
              .find(el => el.innerText.trim().includes(want) && !el.disabled);
    if (b) b.click();
  }, {passive: true});
}
</script>
"""


def _run(loader, actions, show_mail=False):
    """Fetch the queue ONCE into session_state, then page through it locally.

    Streamlit reruns the whole script on every click, so re-querying Turso +
    rescanning 295 folders per click is the lag. Instead we load once and keep a
    cursor (idx) in session_state — Prev/Next (or a swipe) just moves the cursor,
    and acting pops the current card. No re-query per interaction.
    'Refresh' (or reopening the page) re-pulls a fresh queue.
    """
    if SECTION not in st.session_state:
        st.session_state[SECTION] = loader()
    games = st.session_state[SECTION]
    ikey = f"{SECTION}:idx"

    st.button("🔄 Refresh", key=f"{SECTION}:refresh",
              on_click=lambda: (st.session_state.pop(SECTION, None),
                                st.session_state.pop(ikey, None)))

    if not games:
        st.success("Nothing to review here 🎉")
        return

    idx = min(max(st.session_state.get(ikey, 0), 0), len(games) - 1)
    st.session_state[ikey] = idx
    g = review.hydrate(games[idx])   # fetch THIS card's media on demand (cloud); cached after
    _card(g, show_mail)

    # navigation: Prev / Next (swipe on mobile clicks these)
    nav = st.columns(2)
    if nav[0].button("◀ Prev", use_container_width=True, disabled=idx == 0,
                     key="nav_prev"):
        st.session_state[ikey] = idx - 1
        st.rerun()
    if nav[1].button("Next ▶", use_container_width=True, disabled=idx >= len(games) - 1,
                     key="nav_next"):
        st.session_state[ikey] = idx + 1
        st.rerun()

    # actions: act on the CURRENT card, then drop it (next card slides into idx)
    for col, (label, fn) in zip(st.columns(len(actions)), actions):
        if col.button(label, use_container_width=True, key=f"{SECTION}:{label}"):
            warn = fn(g["appid"])           # the only remote round-trip left (~0.16s)
            if warn:
                st.warning(warn)
            games.pop(idx)
            st.session_state[ikey] = min(idx, len(games) - 1) if games else 0
            st.rerun()

    st.caption(f"{idx + 1} / {len(games)}")
    components.html(_SWIPE_JS, height=0)


if SECTION == "Game Approval":
    _run(review.games_to_review,
         [("✅ Accept", review.Accept_Game), ("❌ Reject", _reject)])
elif SECTION == "No-Mail":
    _run(review.nomail_games_to_review, [("❌ Reject", _reject)])
else:
    _run(review.mails_to_review,
         [("✅ Approve", review.Approve_Mail), ("❌ Reject", _reject)], show_mail=True)
