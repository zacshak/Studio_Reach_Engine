"""CLI for one QuickEmailVerification lookup."""

import argparse
import json

try:
    from .client import QEVError, QuickEmailVerification, is_safe_to_send
except ImportError:
    from client import QEVError, QuickEmailVerification, is_safe_to_send


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one email address")
    parser.add_argument("email")
    parser.add_argument("--sandbox", action="store_true", help="use QEV's free mock endpoint")
    args = parser.parse_args()
    try:
        result = QuickEmailVerification.from_env().verify(args.email, sandbox=args.sandbox)
    except (QEVError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if is_safe_to_send(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
