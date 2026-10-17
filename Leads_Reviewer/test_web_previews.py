import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import media_store
from web_previews import upload_previews


def picture(size=(1720, 960), fmt="PNG"):
    image = Image.effect_noise(size, 40).convert("RGB")
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


class Client:
    def __init__(self, original, manifest=None):
        self.original = original
        self.manifest = manifest or {"images": ["hero.png"], "mail": "saved",
                                     "draft_submission": {"revision": 3}, "unknown": [1]}
        self.writes = []
        self.revision = 1
        self.conflict = False
        self.delete = False
        self.missing = False

    def get_object(self, **kwargs):
        if kwargs["Key"].endswith("manifest.json"):
            if self.missing:
                raise S3Error("NoSuchKey")
            return {"Body": io.BytesIO(json.dumps(self.manifest).encode()),
                    "ETag": str(self.revision)}
        return {"Body": io.BytesIO(self.original)}

    def put_object(self, **kwargs):
        if kwargs["Key"].endswith("manifest.json"):
            if self.conflict:
                self.conflict = False
                self.revision += 1
                self.manifest["mail"] = "new concurrent draft"
                self.manifest["unknown"] = [2]
                self.manifest["images"] = ["new.png"]
                self.missing = self.delete
                raise S3Error("PreconditionFailed")
            assert kwargs["IfMatch"] == str(self.revision)
        self.writes.append(kwargs)


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class WebPreviewsTest(unittest.TestCase):
    def test_dimensions_reduction_hashes_and_original_bytes(self):
        for fmt in ("PNG", "JPEG"):
            with self.subTest(fmt=fmt):
                original = picture(fmt=fmt)
                before = hashlib.sha256(original).digest()
                client = Client(original)
                result = upload_previews(client, "bucket", "Game", [("hero", original)])
                entry = result["hero"]
                self.assertEqual((entry["width"], entry["height"]), (860, 480))
                self.assertEqual((entry["thumb_width"], entry["thumb_height"]), (160, 89))
                for write, label, size in zip(client.writes, ("preview", "thumb"),
                                               ((860, 480), (160, 89))):
                    body = write["Body"]
                    digest = hashlib.sha256(body).hexdigest()[:32]
                    self.assertEqual(write["Key"], f"Game/{label}-{digest}.webp")
                    self.assertLess(len(body), len(original))
                    self.assertEqual(write["ContentType"], "image/webp")
                    self.assertIn("immutable", write["CacheControl"])
                    with Image.open(io.BytesIO(body)) as image:
                        self.assertEqual(image.size, size)
                        self.assertEqual(image.format, "WEBP")
                self.assertEqual(hashlib.sha256(original).digest(), before)
                self.assertEqual(result, upload_previews(Client(original), "bucket", "Game", [("hero", original)]))

    def test_small_images_not_upscaled_and_transparency_preserved(self):
        output = io.BytesIO()
        Image.new("RGBA", (30, 90), (100, 20, 30, 0)).save(output, format="PNG")
        client = Client(output.getvalue())
        upload_previews(client, "bucket", "Game", [("small.png", output.getvalue())])
        for write in client.writes:
            with Image.open(io.BytesIO(write["Body"])) as image:
                self.assertEqual(image.size, (30, 90))
                self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_upload_keeps_local_and_remote_originals_and_draft(self):
        original = picture((100, 60))
        client = Client(original)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hero.png"
            path.write_bytes(original)
            with patch("media_readiness.validate_export") as validate:
                media_store.upload_dir(folder, 1, {}, client)
            validate.assert_called_once_with(folder, appid=1)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["hero.png"])
        self.assertEqual(client.writes[0]["Body"], original)
        manifest = json.loads(client.writes[-1]["Body"])
        self.assertEqual(manifest["images"], ["hero.png"])
        self.assertEqual(manifest["mail"], "saved")
        self.assertEqual(manifest["draft_submission"], {"revision": 3})
        self.assertEqual(set(manifest["previews"]), {"hero.png"})

    def test_backfill_preserves_fields_and_retries_current_images(self):
        client = Client(picture((20, 20)))
        client.conflict = True
        result = media_store.backfill_previews("Game", client)
        self.assertEqual(result["mail"], "new concurrent draft")
        self.assertEqual(result["unknown"], [2])
        self.assertEqual(result["draft_submission"], {"revision": 3})
        self.assertEqual(result["images"], ["new.png"])
        self.assertEqual(set(result["previews"]), {"new.png"})
        self.assertEqual(client.writes[-1]["IfMatch"], "2")

    def test_backfill_never_recreates_missing_or_concurrently_deleted_manifest(self):
        for initially_missing in (True, False):
            client = Client(picture((20, 20)))
            client.missing = initially_missing
            client.conflict = client.delete = not initially_missing
            self.assertIsNone(media_store.backfill_previews("Game", client))
            self.assertFalse(any(w["Key"].endswith("manifest.json") for w in client.writes))

    def test_invalid_image_does_not_publish_preview(self):
        client = Client(b"bad")
        with self.assertRaises(OSError):
            upload_previews(client, "bucket", "Game", [("bad.png", b"bad")])
        self.assertEqual(client.writes, [])

    def test_manual_script_selects_only_queue_members(self):
        path = Path(__file__).with_name("backfill_web_previews.py")
        spec = importlib.util.spec_from_file_location("backfill", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = dict(index={"1": "A", "2": "B", "3": "inactive"},
                     approval=[1], mail=[{"appid": 2}], nomail=[], socials=[], triage=[1, 4])
        self.assertEqual(module.queue_folders(state), ["A", "B"])


if __name__ == "__main__":
    unittest.main()
