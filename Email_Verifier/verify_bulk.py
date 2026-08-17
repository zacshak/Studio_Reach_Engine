"""CLI that submits a CSV, waits for verification, and downloads the full report."""

import argparse
import json
from pathlib import Path

try:
    from .client import QEVError, QuickEmailVerification
except ImportError:
    from client import QEVError, QuickEmailVerification


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a CSV email list")
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--output", type=Path, help="full report destination")
    parser.add_argument("--poll-interval", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()

    output = args.output or args.csv_file.with_name(f"{args.csv_file.stem}_qev_report.csv")
    try:
        client = QuickEmailVerification.from_env()
        submitted = client.submit_bulk(args.csv_file)
        job_id = str(submitted.get("id", ""))
        if not job_id:
            raise QEVError("bulk submission did not return a job id")
        result = client.wait_for_bulk(
            job_id, poll_interval=args.poll_interval, timeout=args.timeout
        )
        report_url = result.get("download_urls", {}).get("fullreport", "")
        if not report_url:
            raise QEVError("completed bulk job did not return a full report URL")
        client.download_report(report_url, output)
    except (QEVError, ValueError) as exc:
        parser.error(str(exc))

    summary = {
        "id": job_id,
        "status": result.get("status"),
        "stats": result.get("stats", {}),
        "report": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
