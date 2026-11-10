"""Verification chain used when a game is accepted: own SMTP check, then QEV, then Abstract.

SMTP gives a definitive answer (valid / invalid) for most mailboxes at no cost. Catch-all
domains (the server accepts any address) count as valid: nobody can prove those mailboxes,
and QEV only answers "valid" + accept_all for them. Only the ones SMTP cannot settle
(servers that block probes, timeouts, greylisting) are sent to QuickEmailVerification,
whose free plan allows 100 lookups a day. When QEV is not configured, out of credits or
failing, Abstract API's Email Reputation endpoint (100 free requests a month) takes over
for the rest of the run. When that is missing or exhausted too, those stay "unknown": the
game is not accepted, keeps its email, and can be accepted again later. A fallback error
never turns into "valid".
"""

from __future__ import annotations

import os

from .abstract_client import AbstractEmailReputation, AbstractError
from .client import QEVError, QuickEmailVerification
from .smtp_check import SMTPVerifier


class EmailVerifier:
    def __init__(self, smtp: SMTPVerifier, qev: QuickEmailVerification | None = None,
                 abstract: AbstractEmailReputation | None = None, log=print):
        self.smtp = smtp
        self.qev = qev
        self.abstract = abstract
        self.log = log
        self.qev_calls = 0
        self.abstract_calls = 0

    @classmethod
    def from_env(cls, **kwargs) -> "EmailVerifier":
        smtp = SMTPVerifier(
            helo=os.environ.get("VERIFIER_HELO") or None,
            # The probe never sends mail; MAIL FROM is our own sender so servers that
            # check the reverse path see a real mailbox.
            mail_from=os.environ.get("VERIFIER_MAIL_FROM") or os.environ.get("GMAIL_USER", ""),
        )
        qev = None
        if os.environ.get("QEV_API_KEY", "").strip():
            qev = QuickEmailVerification.from_env(timeout=60)
        abstract = None
        if os.environ.get("ABSTRACT_API_KEY", "").strip():
            abstract = AbstractEmailReputation.from_env(timeout=60)
        return cls(smtp, qev, abstract, **kwargs)

    def verify(self, email: str) -> dict:
        result = self.smtp.verify(email)
        if result["result"] == "unknown" and result.get("accept_all"):
            # Catch-all domain: the server accepts every address, so no checker (QEV
            # included, which answers "valid" + accept_all) can prove the mailbox. Treat it
            # as sendable without spending a QEV credit; a bounce is caught nightly.
            return {**result, "result": "valid", "safe_to_send": True}
        if result["result"] != "unknown":
            return result
        if self.qev is not None:
            fallback = self._ask_qev(email, result)
            if fallback is not None:
                return fallback
        if self.abstract is None:
            return result
        self.log(f"  SMTP inconclusive for {email} ({result['reason']}); asking Abstract")
        try:
            self.abstract_calls += 1
            fallback = self.abstract.verify(email)
        except AbstractError as exc:
            self.log(f"  Abstract exhausted or unavailable ({exc}); "
                     "inconclusive addresses stay unverified")
            self.abstract = None  # stop asking for the rest of this run
            return result
        return {**fallback, "source": "abstract", "smtp_reason": result["reason"]}

    def _ask_qev(self, email: str, result: dict) -> dict | None:
        """QEV's answer, or None once QEV is out of credits or failing (then off for the run)."""
        self.log(f"  SMTP inconclusive for {email} ({result['reason']}); asking QEV")
        try:
            self.qev_calls += 1
            fallback = self.qev.verify(email)
        except QEVError as exc:
            problem = ("daily credits used up" if exc.status_code == 402
                       else f"unavailable ({exc})")
            then = ("switching to Abstract for the rest of the run" if self.abstract
                    else "inconclusive addresses stay unverified")
            self.log(f"  QEV {problem}; {then}")
            self.qev = None  # stop asking for the rest of this run
            return None
        if isinstance(fallback, dict):
            fallback = {**fallback, "source": "qev", "smtp_reason": result["reason"]}
        return fallback
