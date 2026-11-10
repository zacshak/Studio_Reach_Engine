"""CLI for one lookup: own SMTP check, QEV fallback (or --qev / --smtp-only)."""

import argparse
import json

if __package__:
    from .client import QEVError, QuickEmailVerification, is_safe_to_send
    from .smtp_check import SMTPVerifier
    from .verifier import EmailVerifier
else:  # run as a script: python Email_Verifier/<name>.py
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from Email_Verifier.client import QEVError, QuickEmailVerification, is_safe_to_send
    from Email_Verifier.smtp_check import SMTPVerifier
    from Email_Verifier.verifier import EmailVerifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one email address")
    parser.add_argument("email")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smtp-only", action="store_true", help="never call QEV (needs port 25)")
    mode.add_argument("--qev", action="store_true", help="QEV only (spends a credit)")
    parser.add_argument("--sandbox", action="store_true", help="with --qev: QEV's free mock endpoint")
    args = parser.parse_args()
    try:
        if args.qev:
            result = QuickEmailVerification.from_env().verify(args.email, sandbox=args.sandbox)
        else:
            chain = EmailVerifier.from_env()
            if args.smtp_only:
                chain.qev = None
            result = chain.verify(args.email)
    except (QEVError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if is_safe_to_send(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
