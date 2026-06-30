"""Push the cold-mail templates to R2 so they persist and the cloud drafter reads them.

    python SRE.py --sync-templates                 # push the tracked templates file
    python SRE.py --sync-templates path/to/Cold_Mails.txt   # push a specific file

R2 becomes the source of truth (key 'cold_mails.txt'); edit + re-run to update — no redeploy.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "Leads_Reviewer"))
import media_store  # noqa: E402

DEFAULT = os.path.join(HERE, "cold_mail_templates.txt")


def main(argv):
    path = argv[0] if argv else DEFAULT
    if not os.path.isfile(path):
        sys.exit(f"templates file not found: {path}")
    if not media_store.write_enabled():
        sys.exit("R2 write creds missing (R2_BUCKET / R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / "
                 "R2_SECRET_ACCESS_KEY).")
    text = open(path, encoding="utf-8").read()
    media_store.write_templates(text)
    print(f"pushed {len(text)} chars from {os.path.basename(path)} -> R2 "
          f"({media_store.TEMPLATES_KEY})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
