"""Verification chain used by the send stage: own SMTP check first, QEV only as fallback.

SMTP gives a definitive answer (valid / invalid) for most mailboxes at no cost. Catch-all
domains (the server accepts any address) count as valid: nobody can prove those mailboxes,
and QEV only answers "valid" + accept_all for them. Only the ones SMTP cannot settle
(servers that block probes, timeouts, greylisting) are sent to QuickEmailVerification,
whose free plan allows 100 lookups a day. When QEV is not configured, out of credits or
failing, those stay "unknown" and the mail stays Scheduled: such a lead is never sent
unverified and never dropped on an inconclusive answer.
"""

from __future__ import annotations

import os

from .client import QEVError, QuickEmailVerification
from .smtp_check import SMTPVerifier


class EmailVerifier:
    def __init__(self, smtp: SMTPVerifier, qev: QuickEmailVerification | None = None, log=print):
        self.smtp = smtp
        self.qev = qev
        self.log = log
        self.qev_calls = 0

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
        return cls(smtp, qev, **kwargs)

    def verify(self, email: str) -> dict:
        result = self.smtp.verify(email)
        if result["result"] == "unknown" and result.get("accept_all"):
            # Catch-all domain: the server accepts every address, so no checker (QEV
            # included, which answers "valid" + accept_all) can prove the mailbox. Treat it
            # as sendable without spending a QEV credit; a bounce is caught nightly.
            return {**result, "result": "valid", "safe_to_send": True}
        if result["result"] != "unknown" or self.qev is None:
            return result
        self.log(f"  SMTP inconclusive for {email} ({result['reason']}); asking QEV")
        try:
            self.qev_calls += 1
            fallback = self.qev.verify(email)
        except QEVError as exc:
            if exc.status_code == 402:
                self.log("  QEV daily credits used up; inconclusive addresses stay Scheduled")
            else:
                self.log(f"  QEV unavailable ({exc}); inconclusive addresses stay Scheduled")
            self.qev = None  # stop asking for the rest of this run
            return result
        if isinstance(fallback, dict):
            fallback = {**fallback, "source": "qev", "smtp_reason": result["reason"]}
        return fallback
