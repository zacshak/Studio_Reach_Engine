"""Kimchi's interface to the lead database.

This is the ONLY module Kimchi imports. It exposes the three calls Kimchi needs
and nothing else — the database schema, triggers, connections, status seeding
and read-only enforcement all live in the owning `pipeline.py` (in the
Claude_Lead_Discovery_Engine folder) and are deliberately hidden here.

    import scraper_interface as leads

    for appid in leads.get_pending():
        row = leads.read_lead(appid)
        ... scrape ...
        leads.write_result(appid, scrape_status="SCRAPED", emails="a@b.com")
"""
import os
import sys

# pipeline.py lives in the sibling System 1 folder (the DB owner). Put it on the
# path so we can re-export its public calls without copying any logic.
_PIPELINE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Claude_Lead_Discovery_Engine")
sys.path.insert(0, _PIPELINE_DIR)
import pipeline  # noqa: E402


def get_pending(limit=None):
    """Appids that still need scraping (status 'pending'). Your work queue.
    Pass limit=N to take only the first N (e.g. limit=5 for a test run)."""
    return pipeline.get_pending(limit)


def read_lead(appid):
    """The full lead row for one appid as a dict (read-only). None if unknown."""
    return pipeline.read_lead(appid)


def write_result(appid, *, scrape_status, emails=None, website=None):
    """Save your result for one appid. Writes the results table only.
    scrape_status is one of: 'SCRAPED', 'no_email', 'failed'."""
    return pipeline.write_result(
        appid, scrape_status=scrape_status, emails=emails, website=website)
