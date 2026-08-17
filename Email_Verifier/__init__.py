"""Standalone QuickEmailVerification client; not connected to the lead pipeline."""

from .client import QEVError, QuickEmailVerification, is_safe_to_send

__all__ = ["QEVError", "QuickEmailVerification", "is_safe_to_send"]
