import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image

from media_readiness import validate_export, SPRITE_NAME
import media_store
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'Claude_Lead_Discovery_Engine'))
import approval_export


class MediaReadinessTest(unittest.TestCase):
    def test_partial_folder_is_never_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            Image.new('RGB',(4,4)).save(os.path.join(folder,'screenshot_00.jpg'))
            client = Mock()
            with self.assertRaisesRegex(ValueError,'completion marker'):
                media_store.upload_dir(folder,1,{},client)
            client.put_object.assert_not_called()

    def test_marker_alone_does_not_make_export_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder,'.complete'),'w').close()
            with self.assertRaisesRegex(ValueError,'sprite sheet missing'): validate_export(folder)
            Image.new('RGB',(4,4)).save(os.path.join(folder,SPRITE_NAME))
            with open(os.path.join(folder,'screenshot_00.jpg'),'wb') as file: file.write(b'broken')
            with self.assertRaises(OSError): validate_export(folder)

    def test_zero_screenshots_never_marks_complete(self):
        with tempfile.TemporaryDirectory() as root:
            exporter = approval_export.ApprovalExporter(base_dir=root)
            with patch.object(approval_export,'_scrape_status',return_value='seeded'), patch.object(approval_export.pipeline,'read_lead',return_value={'name':'Synthetic','screenshots':'[]'}):
                self.assertFalse(exporter._export_one(1))
            exporter.close()
            self.assertFalse(os.path.exists(os.path.join(root,'Synthetic_1','.complete')))

    def test_stale_marker_and_corrupt_download_are_repaired_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root,'Synthetic_1'); os.mkdir(folder)
            open(os.path.join(folder,'.complete'),'w').close()
            path = os.path.join(folder,'screenshot_00.jpg')
            with open(path,'wb') as file: file.write(b'partial old download')
            image = io.BytesIO(); Image.new('RGB',(4,4)).save(image,format='JPEG')
            exporter = approval_export.ApprovalExporter(base_dir=root)
            with patch.object(approval_export,'_scrape_status',return_value='seeded'), patch.object(approval_export.pipeline,'read_lead',return_value={'name':'Synthetic','screenshots':json.dumps([{'path_thumbnail':'https://example.test/a.jpg'}])}), patch.object(approval_export.urllib.request,'urlopen',return_value=io.BytesIO(image.getvalue())):
                self.assertTrue(exporter._export_one(1))
            exporter.close()
            validate_export(folder)
            self.assertFalse(os.path.exists(path+'.part'))


if __name__ == '__main__': unittest.main()
