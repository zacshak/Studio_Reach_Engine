"""Reconcile unsent leads with missing, invalid, or corrected recipients."""
import pipeline


def main():
    appids = pipeline.repair_invalid_rows()
    print(f"reconciled {len(appids)} unsent lead(s)"
          + (f": {', '.join(map(str, appids))}" if appids else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
