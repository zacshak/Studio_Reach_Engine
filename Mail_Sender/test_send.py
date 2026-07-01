"""One-off spam-placement check: send a single cold-mail-style message through the SAME
Gmail SMTP path the real outreach uses — identical to mailer._send (same From name,
STARTTLS on smtp.gmail.com:587, plain-text single-recipient MIME) — so where it lands
(inbox vs spam) reflects the real thing. Stdlib only; no DB / R2 needed.

    python Mail_Sender/test_send.py [recipient]     # default: recipient@example.com

Env: GMAIL_USER + GMAIL_APP_PASSWORD (the same GHA secrets the send job uses).
"""
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

SENDER_NAME = "Meshak"                 # keep in sync with mailer.SENDER_NAME
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
DEFAULT_TO = "recipient@example.com"

SUBJECT = "helping with your game ?"
BODY = """Hi,

I came across your upcoming game on Steam and really liked the direction it's taking.
I'm a freelance game programmer (gameplay systems, tools, optimisation) and I help small
studios ship faster without adding headcount.

If you're short-handed on the programming side, I'd be glad to lend a hand. Happy to
share a couple of things I've shipped if that's useful.

Best,
Meshak
"""


def main(argv):
    user = os.environ.get("GMAIL_USER", "you@example.com")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not password:
        sys.exit("GMAIL_APP_PASSWORD not set (env / GHA secret).")
    to = (argv[0] if argv else DEFAULT_TO).strip() or DEFAULT_TO

    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{user}>"
    msg["To"] = to
    msg["Subject"] = SUBJECT
    msg.set_content(BODY)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)
    print(f"sent test mail to {to} from {user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
