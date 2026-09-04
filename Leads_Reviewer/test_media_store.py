import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

import media_store


class MediaStoreTest(unittest.TestCase):
    def test_strict_reads_fail_instead_of_erasing_remote_state(self):
        with patch.object(media_store, "write_enabled", return_value=False), \
                patch.object(media_store.urllib.request, "urlopen", side_effect=URLError("offline")):
            self.assertEqual(media_store.fetch_index(), {})
            with self.assertRaises(media_store.MediaStoreError):
                media_store.fetch_index(strict=True)

    def test_reads_use_private_client_when_credentials_exist(self):
        class Client:
            def get_object(self, **_kwargs):
                return {"Body": io.BytesIO(b'{"1":"Game_1"}')}

        with patch.object(media_store, "write_enabled", return_value=True), \
                patch.object(media_store, "_client", return_value=Client()), \
                patch.object(media_store.urllib.request, "urlopen",
                             side_effect=AssertionError("public URL used")):
            self.assertEqual(media_store.fetch_index(strict=True), {"1": "Game_1"})

    def test_shared_json_update_retries_without_losing_concurrent_data(self):
        class Conflict(Exception):
            response = {"Error": {"Code": "PreconditionFailed"}}

        class Client:
            def __init__(self):
                self.reads = 0
                self.written = None

            def get_object(self, **_kwargs):
                self.reads += 1
                value = {"existing": 1}
                if self.reads > 1:
                    value["concurrent"] = 2
                return {"Body": io.BytesIO(json.dumps(value).encode()),
                        "ETag": f'"v{self.reads}"'}

            def put_object(self, **kwargs):
                if self.reads == 1:
                    raise Conflict()
                self.written = json.loads(kwargs["Body"])

        client = Client()
        result = media_store.update_index(
            lambda current: {**current, "ours": 3}, client=client)
        self.assertEqual(result, {"existing": 1, "concurrent": 2, "ours": 3})
        self.assertEqual(client.written, result)

    def test_delete_keeps_index_until_objects_are_gone(self):
        events = []

        class Paginator:
            def paginate(self, **_kwargs):
                return [{"Contents": [{"Key": "Game_1/image.jpg"}]}]

        class Client:
            def get_object(self, **_kwargs):
                return {"Body": io.BytesIO(b'{"1":"Game_1"}'), "ETag": '"v1"'}

            def get_paginator(self, _name):
                return Paginator()

            def delete_objects(self, **_kwargs):
                events.append("delete")
                return {}

            def put_object(self, **kwargs):
                events.append(("index", json.loads(kwargs["Body"])))
                return {}

        self.assertTrue(media_store.delete_lead_media(1, client=Client()))
        self.assertEqual(events, ["delete", ("index", {})])


if __name__ == "__main__":
    unittest.main()
