"""Microsoft 365 sign-in (OAuth 2.0) for sending accounts: SMTP and IMAP with XOAUTH2.

The refresh token is stored encrypted in the database (oauth_tokens) with a key derived
from the MS_TOKEN_KEY secret; it is renewed on every use, so it never ages out while the
account is used at least every 90 days. No password is ever stored.

Connect a mailbox once (device sign-in; prints a code to enter at microsoft.com/devicelogin):
    python Mail_Sender/ms_oauth.py --connect you@yourdomain.com
"""
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
import pipeline  # noqa: E402

# The "SRE Mailer" app registration in the xrevise.cc tenant. Not secrets.
CLIENT_ID = os.environ.get("MS_CLIENT_ID") or "452aad1d-5427-4ae5-82fc-5918373faa13"
TENANT_ID = os.environ.get("MS_TENANT_ID") or "fd42f542-0db0-4257-b0b3-86217a7aaad9"
SCOPES = ("https://outlook.office.com/SMTP.Send "
          "https://outlook.office.com/IMAP.AccessAsUser.All offline_access openid")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0"
SMTP = ("smtp.office365.com", 587)
IMAP = "outlook.office365.com"

_cache = {}  # email -> (access_token, expires_at) for this process


class MSAuthError(RuntimeError):
    pass


def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("MS_TOKEN_KEY", "")
    if len(secret) < 16:
        raise MSAuthError("MS_TOKEN_KEY secret is missing or shorter than 16 characters")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _post(url, fields):
    req = urllib.request.Request(url, urllib.parse.urlencode(fields).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except ValueError:
            raise MSAuthError(f"token endpoint HTTP {exc.code}") from None


def _save(email, refresh_token):
    pipeline.set_oauth_token(email, _fernet().encrypt(refresh_token.encode()).decode())


def connected(email):
    """True when a token is stored and the key to read it is present."""
    try:
        return bool(pipeline.get_oauth_token(email)) and len(os.environ.get("MS_TOKEN_KEY", "")) >= 16
    except Exception:
        return False


def access_token(email):
    """A fresh access token for the mailbox; renews and re-stores the refresh token."""
    cached = _cache.get(email.lower())
    if cached and cached[1] > time.time() + 120:
        return cached[0]
    blob = pipeline.get_oauth_token(email)
    if not blob:
        raise MSAuthError(f"{email} is not connected (run the Microsoft connect workflow)")
    try:
        refresh = _fernet().decrypt(blob.encode()).decode()
    except Exception:
        raise MSAuthError("stored token cannot be decrypted (MS_TOKEN_KEY changed?)") from None
    data = _post(f"{AUTHORITY}/token", dict(client_id=CLIENT_ID, grant_type="refresh_token",
                                            refresh_token=refresh, scope=SCOPES))
    if "access_token" not in data:
        raise MSAuthError(f"token refresh failed: {data.get('error')}: "
                          f"{str(data.get('error_description', ''))[:160]}")
    if data.get("refresh_token"):
        _save(email, data["refresh_token"])
    _cache[email.lower()] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def xoauth2(email):
    return f"user={email}\x01auth=Bearer {access_token(email)}\x01\x01"


def smtp_login(smtp, email):
    """Authenticate an SMTP connection (after STARTTLS) with XOAUTH2."""
    smtp.ehlo()
    smtp.auth("XOAUTH2", lambda challenge=None: xoauth2(email), initial_response_ok=True)


def imap_login(imap, email):
    imap.authenticate("XOAUTH2", lambda _challenge: xoauth2(email).encode())


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def connect(email, *, log=print, sleep=time.sleep):
    """Device sign-in for one mailbox; stores its encrypted refresh token."""
    _fernet()  # fail before showing a code if the key is missing
    start = _post(f"{AUTHORITY}/devicecode", dict(client_id=CLIENT_ID, scope=SCOPES))
    if "device_code" not in start:
        raise MSAuthError(f"device sign-in could not start: {start.get('error')}: "
                          f"{str(start.get('error_description', ''))[:200]}")
    log(f"SIGN IN AS {email}: open {start['verification_uri']} and enter code {start['user_code']}")
    log(start.get("message", ""))
    deadline = time.time() + int(start.get("expires_in", 900))
    interval = int(start.get("interval", 5))
    while time.time() < deadline:
        sleep(interval)
        data = _post(f"{AUTHORITY}/token", dict(
            client_id=CLIENT_ID, device_code=start["device_code"],
            grant_type="urn:ietf:params:oauth:grant-type:device_code"))
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise MSAuthError(f"sign-in failed: {error}: {str(data.get('error_description', ''))[:200]}")
        who = (_claims(data.get("id_token", "")).get("preferred_username") or "").lower()
        if who and who != email.lower():
            raise MSAuthError(f"signed in as {who}, not {email}. Nothing was saved; run it again "
                              "and sign in with the right mailbox (a private window helps).")
        if not data.get("refresh_token"):
            raise MSAuthError("no refresh token returned (is offline_access granted?)")
        _save(email, data["refresh_token"])
        _cache[email.lower()] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        log(f"CONNECTED {email}")
        return True
    raise MSAuthError("the code expired before sign-in finished; run it again")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--connect":
        try:
            connect(args[1].strip())
        except MSAuthError as exc:
            sys.exit(f"ERROR: {exc}")
    else:
        sys.exit("usage: ms_oauth.py --connect EMAIL")
