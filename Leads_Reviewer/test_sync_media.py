import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_media


class CleanupTest(unittest.TestCase):
    def test_cleanup_deletes_unowned_objects_before_rewriting_index(self):
        events = []
        index = {"1": "keep_1", "2": "drop_2", "3": "missing_3"}

        class Paginator:
            def paginate(self, **_kwargs):
                return [{"Contents": [
                    {"Key": "index.json", "Size": 20},
                    {"Key": "keep_1/manifest.json", "Size": 10},
                    {"Key": "drop_2/manifest.json", "Size": 11},
                    {"Key": "orphan_4/image.jpg", "Size": 12},
                ]}]

        class Client:
            def get_object(self, **kwargs):
                value = index if kwargs["Key"] == "index.json" else []
                return {"Body": io.BytesIO(json.dumps(value).encode()), "ETag": '"v1"'}

            def get_paginator(self, _name):
                return Paginator()

            def delete_objects(self, **kwargs):
                events.append(("delete", [item["Key"] for item in kwargs["Delete"]["Objects"]]))
                return {}

            def put_object(self, **kwargs):
                events.append(("put", kwargs["Key"], json.loads(kwargs["Body"])))
                return {}

        with patch.object(sync_media, "_active_appids", return_value={1}):
            self.assertEqual(sync_media.cleanup_r2(Client(), apply=True), 2)
        self.assertEqual(events[0][0], "delete")
        self.assertEqual(events[1], ("put", "index.json", {"1": "keep_1"}))


if __name__ == "__main__":
    unittest.main()
