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


get_pending = pipeline.get_pending
read_lead = pipeline.read_lead
write_result = pipeline.write_result
