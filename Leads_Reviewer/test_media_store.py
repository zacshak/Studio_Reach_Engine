import io
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

import media_store


class MediaStoreTest(unittest.TestCase):
    def test_recovery_checks_published_input_versions(self):
        class Client:
            def head_object(self, **kwargs):
                return {'ETag':'"current"'}
        submission={'inputs':[{'key':'cold_mails.txt','etag':'current'}, {'key':'Game/sprite.png','etag':'current'}]}
        media_store.check_draft_inputs(submission,client=Client())
        submission['inputs'][0]['etag']='old'
        with self.assertRaises(media_store.MediaStoreError):
            media_store.check_draft_inputs(submission,client=Client())

    def test_draft_write_preserves_latest_images_and_does_not_recreate_deleted_manifest(self):
        class Missing(Exception):
            response = {'Error': {'Code': 'NoSuchKey'}}
        class Client:
            written = None
            missing = False
            def get_object(self, **kwargs):
                if self.missing:
                    raise Missing()
                return {'Body': io.BytesIO(b'{"images":["new.png"],"name":"New"}'), 'ETag':'v2'}
            def put_object(self, **kwargs):
                self.written = json.loads(kwargs['Body'])
        client = Client()
        draft = {'images':['old.png'], 'mail':'Approved text', 'mail_template':2}
        media_store.write_manifest('Game_1', draft, client=client)
        self.assertEqual(client.written['images'], ['new.png'])
        self.assertEqual(client.written['mail'], 'Approved text')
        client.missing = True
        client.written = None
        with self.assertRaises(media_store.MediaStoreError):
            media_store.write_manifest('Game_1', draft, client=client)
        self.assertIsNone(client.written)

    def test_gha_cannot_replace_an_mcp_draft(self):
        saved = {'mail':'MCP draft','mail_template':1,'draft_submission':{'revision':'saved'}}
        class Client:
            def get_object(self, **kwargs):
                return {'Body':io.BytesIO(json.dumps(saved).encode()),'ETag':'v1'}
            def put_object(self, **kwargs):
                self.written=json.loads(kwargs['Body'])
        client=Client()
        with self.assertRaises(media_store.MediaStoreError):
            media_store.write_manifest('Game_1',{'mail':'Other draft','mail_template':1},client=client)
        result=media_store.write_manifest('Game_1',{'mail':'MCP draft','mail_template':1},client=client)
        self.assertEqual(result,saved)

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
