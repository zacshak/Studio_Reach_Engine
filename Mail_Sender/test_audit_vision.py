import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import audit_vision


class AuditVisionTest(unittest.TestCase):
    def test_audit_reports_pass_and_unverified_without_aborting_other_routes(self):
        error = audit_vision.runtime.ProviderUnavailable('unavailable')
        with patch.dict(os.environ, {'CHECK_MODELS': '["route-a","route-b"]', 'GITHUB_STEP_SUMMARY': ''}), patch.object(audit_vision.runtime, 'configured_client', return_value=(None, 'default')), patch.object(audit_vision.runtime, 'preflight', side_effect=[None, error]) as probe, patch.object(audit_vision.runtime, 'ATTEMPTS', 3), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(audit_vision.main(), 0)
            self.assertEqual(probe.call_count, 2)
            self.assertIn('image_and_json_passed', output.getvalue())
            self.assertIn('unverified', output.getvalue())

    def test_invalid_input_never_calls_provider(self):
        with patch.dict(os.environ, {'CHECK_MODELS': '[]'}), patch.object(audit_vision.runtime, 'configured_client') as client:
            with self.assertRaises(ValueError): audit_vision.main()
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
