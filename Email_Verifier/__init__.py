"""Email verification: own SMTP check first, QuickEmailVerification as fallback."""

from .client import QEVError, QuickEmailVerification, is_safe_to_send
from .smtp_check import SMTPVerifier, classify_rcpt
from .verifier import EmailVerifier

__all__ = ["EmailVerifier", "QEVError", "QuickEmailVerification", "SMTPVerifier",
           "classify_rcpt", "is_safe_to_send"]
