"""Free mailbox verification: syntax -> DNS (MX) -> SMTP RCPT probe -> catch-all probe.

No mail is ever sent: the probe stops after RCPT TO and closes with RSET/QUIT.
Needs outbound TCP port 25 (open on GitHub-hosted runners, blocked on most home ISPs,
Cloudflare Workers and this repo's dev sandbox).

Results use QuickEmailVerification's shape, so callers and the verification cache need
no changes: {"email", "result": valid|invalid|unknown, "reason", "accept_all", "source"}.
Only an explicit "no such mailbox / domain" answer is "invalid". Anything the server
answers for policy reasons (IP blocklists, SPF, rate limits), temporary failures and
catch-all domains are "unknown": never a reason to drop a lead, never a reason to send.
"""

from __future__ import annotations

import re
import secrets
import smtplib
import socket
from dataclasses import dataclass

import dns.exception
import dns.resolver

# Deliberately loose on the local part (RFC 5321 allows a lot); strict enough on the domain.
_EMAIL = re.compile(r"^[^@\s]{1,64}@(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                    r"[A-Za-z]{2,63}$")
_ENHANCED = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
# Enhanced status codes that mean "this mailbox does not exist / cannot receive".
# 5.1.x = addressing (bad mailbox/domain), 5.2.1 = mailbox disabled.
# 5.2.2 (mailbox full) is deliberately NOT here: the address exists.
_NO_MAILBOX_CODES = {"5.1.0", "5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.10", "5.2.1"}
# Wording used by servers that reply 550 without an enhanced code. Policy rejections
# ("blocked", "spamhaus", "policy", "SPF", "reputation") must never match.
_NO_MAILBOX_TEXT = re.compile(
    r"(user|mailbox|recipient|address|account)\s*(name\s*)?(is\s*)?"
    r"(unknown|not\s*found|does\s*not\s*exist|doesn'?t\s*exist|unavailable|invalid|disabled|"
    r"not\s*exist|no\s*longer)|no\s*such\s*(user|mailbox|recipient|address)|"
    r"unknown\s*(user|recipient|mailbox)|invalid\s*recipient|user\s*not\s*local",
    re.I)
_POLICY_TEXT = re.compile(r"block|spam|policy|reputation|blacklist|denylist|rbl|spf|dmarc|"
                          r"not\s*permitted|access\s*denied|rate|too\s*many|try\s*again|"
                          r"authenticat|relay", re.I)


@dataclass
class _Probe:
    code: int | None
    message: str


def _result(email, result, reason, *, accept_all=False, mx=None):
    out = {"email": email, "result": result, "reason": reason, "accept_all": accept_all,
           "safe_to_send": result == "valid", "source": "smtp"}
    if mx:
        out["mx"] = mx
    return out


def classify_rcpt(code: int | None, message: str) -> str:
    """Map one RCPT reply to valid / invalid / unknown."""
    if code is None:
        return "unknown"
    if 200 <= code < 300:
        return "valid"
    if code < 500:
        return "unknown"  # 4xx: greylisting, rate limit, temporary failure
    enhanced = _ENHANCED.search(message or "")
    if enhanced:
        status = ".".join(enhanced.groups())
        if status in _NO_MAILBOX_CODES:
            return "invalid"
        if status.startswith("5.7.") or status.startswith("5.4.") or status.startswith("5.3."):
            return "unknown"
    if code in (550, 551, 553) and _NO_MAILBOX_TEXT.search(message or "") \
            and not _POLICY_TEXT.search(message or ""):
        return "invalid"
    return "unknown"


class SMTPVerifier:
    def __init__(self, *, helo: str | None = None, mail_from: str = "", timeout: float = 15,
                 resolver=None, smtp_factory=None, max_mx: int = 2):
        self.helo = helo or socket.getfqdn() or "localhost"
        self.mail_from = mail_from
        self.timeout = timeout
        self.max_mx = max_mx
        self._resolver = resolver or dns.resolver.Resolver()
        self._resolver.lifetime = timeout
        self._smtp = smtp_factory or (lambda: smtplib.SMTP(timeout=self.timeout))
        self._domains: dict[str, tuple] = {}  # per-run domain cache: (mx list | verdict)

    # ---- DNS ---------------------------------------------------------------------------
    def _mx_hosts(self, domain: str):
        """Return (hosts, None) or (None, (result, reason))."""
        if domain in self._domains:
            return self._domains[domain]
        try:
            answers = self._resolver.resolve(domain, "MX")
            records = sorted((r.preference, str(r.exchange).rstrip(".")) for r in answers)
            hosts = [h for _, h in records if h]
            # RFC 7505 null MX: "this domain accepts no mail".
            out = (None, ("invalid", "null_mx")) if not hosts else (hosts, None)
        except dns.resolver.NXDOMAIN:
            out = (None, ("invalid", "domain_does_not_exist"))
        except dns.resolver.NoAnswer:
            # RFC 5321 implicit MX: no MX record -> deliver to the domain's A/AAAA record.
            out = self._implicit_mx(domain)
        except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.exception.DNSException):
            return None, ("unknown", "dns_error")  # not cached: retry next run
        self._domains[domain] = out
        return out

    def _implicit_mx(self, domain):
        for rtype in ("A", "AAAA"):
            try:
                self._resolver.resolve(domain, rtype)
                return [domain], None
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except dns.exception.DNSException:
                return None, ("unknown", "dns_error")
        return None, ("invalid", "no_mail_server")

    # ---- SMTP --------------------------------------------------------------------------
    def _session(self, host: str, rcpts: list[str]) -> list[_Probe] | _Probe:
        """One SMTP conversation; RCPT each address (RSET between). Never sends DATA."""
        smtp = self._smtp()
        try:
            code, msg = smtp.connect(host, 25)
            if code != 220:
                return _Probe(None, f"banner {code}")
            code, msg = smtp.ehlo(self.helo)
            if not 200 <= code < 300:
                code, msg = smtp.helo(self.helo)
                if not 200 <= code < 300:
                    return _Probe(None, f"helo {code}")
            if smtp.has_extn("starttls"):
                try:
                    smtp.starttls()
                    smtp.ehlo(self.helo)
                except (smtplib.SMTPException, OSError):
                    pass  # plain-text probe is fine; nothing secret is exchanged
            replies = []
            for i, rcpt in enumerate(rcpts):
                if i:
                    smtp.rset()
                code, msg = smtp.mail(self.mail_from)
                if not 200 <= code < 300:
                    return _Probe(None, f"mail_from_rejected {code} {_text(msg)}")
                code, msg = smtp.rcpt(rcpt)
                replies.append(_Probe(code, _text(msg)))
                if not 200 <= code < 300:
                    break  # the real address was refused: catch-all probe is pointless
            return replies
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPException, OSError) as exc:
            return _Probe(None, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass

    def verify(self, email: str) -> dict:
        email = (email or "").strip()
        if not _EMAIL.match(email):
            return _result(email, "invalid", "bad_syntax")
        domain = email.rsplit("@", 1)[1].lower()
        hosts, verdict = self._mx_hosts(domain)
        if verdict:
            return _result(email, *verdict)

        last = "no_mx_reachable"
        for host in hosts[: self.max_mx]:
            canary = f"no-such-user-{secrets.token_hex(6)}@{domain}"
            replies = self._session(host, [email, canary])
            if isinstance(replies, _Probe):  # connection-level failure: try the next MX
                last = f"smtp_unreachable ({replies.message})"[:200]
                continue
            real = replies[0]
            outcome = classify_rcpt(real.code, real.message)
            if outcome == "invalid":
                return _result(email, "invalid", f"mailbox_does_not_exist ({real.code})", mx=host)
            if outcome == "unknown":
                return _result(email, "unknown", f"smtp_{real.code} {real.message}"[:200], mx=host)
            canary = replies[1] if len(replies) > 1 else _Probe(None, "")
            canary_outcome = classify_rcpt(canary.code, canary.message)
            if canary_outcome == "valid":
                # The server accepted a random address too: it accepts everything, so a
                # 250 for ours proves nothing.
                return _result(email, "unknown", "accept_all", accept_all=True, mx=host)
            if canary_outcome == "unknown":
                return _result(email, "unknown", f"catch_all_check_inconclusive ({canary.code})",
                               mx=host)
            return _result(email, "valid", "mailbox_exists", mx=host)
        return _result(email, "unknown", last)


def _text(msg) -> str:
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return " ".join(str(msg).split())
