"""Focused checks for the DB-backed triage handoff."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import triage_cloud  # noqa: E402


class TriageCloudTest(unittest.TestCase):
    def test_pending_appids_resolve_to_r2_manifests(self):
        manifests = {"Game_7": {"name": "Game", "images": ["SpriteSheet.png"]}}
        with patch.object(triage_cloud.pipeline, "mail_status_appids", return_value=[7]), \
                patch.object(triage_cloud.media_store, "fetch_index",
                             return_value={"7": "Game_7"}), \
                patch.object(triage_cloud.media_store, "fetch_manifest",
                             side_effect=manifests.get):
            self.assertEqual(triage_cloud._games(), [(7, "Game_7", manifests["Game_7"])])

    def test_missing_pending_media_fails(self):
        with patch.object(triage_cloud.pipeline, "mail_status_appids", return_value=[7]), \
                patch.object(triage_cloud.media_store, "fetch_index", return_value={}):
            with self.assertRaises(RuntimeError):
                triage_cloud._games()


if __name__ == "__main__":
    unittest.main()
