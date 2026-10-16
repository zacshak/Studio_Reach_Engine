import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import audit_media


class AuditMediaTest(unittest.TestCase):
    def row(self, status='Writing'):
        return {'appid':1, 'Mail_status':status, 'scrape_status':'seeded', 'emails':'studio@example.com'}

    def test_missing_index_and_incomplete_manifest_are_reported(self):
        self.assertEqual(audit_media.inspect(self.row(),{}),['missing_media_index'])
        with patch.object(audit_media,'request',return_value={'images':[]}):
            self.assertEqual(audit_media.inspect(self.row(),{'1':'Game_1'}),['sprite_not_in_manifest'])

    def test_missing_object_differs_from_access_failure(self):
        for status, expected in [(404,'missing_image'),(403,'image_check_error')]:
            with patch.object(audit_media,'request',side_effect=[{'images':['SpriteSheet_Screenshots.png']},HTTPError('url',status,'test',{},None)]):
                issues=audit_media.inspect(self.row(),{'1':'Game_1'})
                self.assertEqual(issues,[expected+':SpriteSheet_Screenshots.png'])

    def test_drafted_lead_requires_saved_mail(self):
        with patch.object(audit_media,'request',side_effect=[{'images':['SpriteSheet_Screenshots.png']},{'bytes':100}]):
            self.assertEqual(audit_media.inspect(self.row('Drafted'),{'1':'Game_1'}),['missing_draft_content'])


if __name__ == '__main__': unittest.main()
