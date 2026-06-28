"""Game-Approval screen, Streamlit POC. Pure Python — no JS, no API layer.

    pip install streamlit pillow
    streamlit run Web_POC/streamlit_app.py     # opens in browser; phone-friendly

Streamlit reruns this whole script on every interaction; that's why a button just
mutates state and the page redraws itself. DRY=True -> clicks only advance, no DB
writes. Flip DRY=False to actually Accept/Reject (writes DB + deletes media on reject).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Leads_Reviewer"))
import Reviewer_Interface as review  # noqa: E402
import streamlit as st               # noqa: E402

DRY = True   # ponytail: safe default. set False to hit the real DB.

st.set_page_config(page_title="Game Approval", layout="centered")
st.title("Game Approval" + ("  ·  DRY" if DRY else ""))

games = review.games_to_review()
i = st.session_state.setdefault("i", 0)
if i >= len(games):
    st.success("Nothing left to review 🎉")
    st.stop()

g = games[i]
st.subheader(g["name"])
st.caption(g["meta"])
sheet = next((s for s in g["shots"] if "SpriteSheet" in s),
             g["shots"][0] if g["shots"] else None)
if sheet:
    st.image(sheet, use_container_width=True)
st.write(g["desc"])

c1, c2 = st.columns(2)
if c1.button("✅ Accept", use_container_width=True):
    if not DRY:
        review.Accept_Game(g["appid"])   # status -> 'Writing', leaves the list
    else:
        st.session_state.i += 1
    st.rerun()
if c2.button("❌ Reject", use_container_width=True):
    if not DRY:
        review.Reject_Game(g["appid"])   # deletes DB rows + media folder
    else:
        st.session_state.i += 1
    st.rerun()

st.caption(f"{len(games) - i} to go")
