"""CLI that verifies every address in a CSV and writes a report CSV.

Default: our own SMTP check per address, QEV only for inconclusive ones (same chain as the
pipeline; needs outbound port 25). --qev uploads the whole file to QuickEmailVerification's
bulk API instead (spends one credit per row).

    python Email_Verifier/verify_bulk.py leads.csv [--output report.csv] [--smtp-only | --qev]
"""

import argparse
import collections
import csv
import json
from pathlib import Path

if __package__:
    from .client import QEVError, QuickEmailVerification
    from .verifier import EmailVerifier
else:  # run as a script: python Email_Verifier/<name>.py
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from Email_Verifier.client import QEVError, QuickEmailVerification
    from Email_Verifier.verifier import EmailVerifier

REPORT_FIELDS = ("result", "reason", "accept_all", "source")


def email_column(fieldnames):
    for name in fieldnames or []:
        if name and name.strip().lower() in ("email", "emails", "e-mail", "email address"):
            return name
    raise ValueError("CSV needs an 'email' column")


def verify_csv(source: Path, output: Path, verifier) -> dict:
    """Verify each row's first address; write the rows back with the result columns."""
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        column = email_column(reader.fieldnames)
        rows = list(reader)
        fields = list(reader.fieldnames) + [f for f in REPORT_FIELDS if f not in reader.fieldnames]
    seen, counts = {}, collections.Counter()
    for row in rows:
        address = (row.get(column) or "").split(",", 1)[0].strip()
        key = address.casefold()
        if key not in seen:
            seen[key] = verifier.verify(address) if address else {
                "result": "invalid", "reason": "empty", "source": "csv"}
        result = seen[key]
        row.update({f: result.get(f, "") for f in REPORT_FIELDS})
        counts[str(result.get("result"))] += 1
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return {"rows": len(rows), "unique": len(seen), "results": dict(counts), "report": str(output)}


def qev_bulk(source: Path, output: Path, poll_interval: float, timeout: float) -> dict:
    client = QuickEmailVerification.from_env()
    job_id = str(client.submit_bulk(source).get("id", ""))
    if not job_id:
        raise QEVError("bulk submission did not return a job id")
    result = client.wait_for_bulk(job_id, poll_interval=poll_interval, timeout=timeout)
    report_url = result.get("download_urls", {}).get("fullreport", "")
    if not report_url:
        raise QEVError("completed bulk job did not return a full report URL")
    client.download_report(report_url, output)
    return {"id": job_id, "status": result.get("status"), "stats": result.get("stats", {}),
            "report": str(output)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify a CSV email list")
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--output", type=Path, help="report destination")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smtp-only", action="store_true", help="never call QEV")
    mode.add_argument("--qev", action="store_true", help="QEV bulk API for the whole file")
    parser.add_argument("--poll-interval", type=float, default=10, help="with --qev")
    parser.add_argument("--timeout", type=float, default=3600, help="with --qev")
    args = parser.parse_args(argv)
    if not args.csv_file.is_file():
        parser.error(f"CSV file does not exist: {args.csv_file}")
    suffix = "qev_report" if args.qev else "verified"
    output = args.output or args.csv_file.with_name(f"{args.csv_file.stem}_{suffix}.csv")
    try:
        if args.qev:
            summary = qev_bulk(args.csv_file, output, args.poll_interval, args.timeout)
        else:
            verifier = EmailVerifier.from_env()
            if args.smtp_only:
                verifier.qev = None
            summary = verify_csv(args.csv_file, output, verifier)
    except (QEVError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
