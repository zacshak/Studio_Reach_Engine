import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from Email_Verifier.client import QEVError, QuickEmailVerification, is_safe_to_send


class Response(io.BytesIO):
    def __init__(self, body=b"{}"):
        super().__init__(body)
        self.headers = {}


class QuickEmailVerificationTest(unittest.TestCase):
    def test_single_uses_sandbox_and_preserves_provider_result(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return Response(json.dumps({"success": "true", "safe_to_send": "true"}).encode())

        result = QuickEmailVerification("secret", opener=open_request).verify(
            "safe-to-send@example.com", sandbox=True
        )

        self.assertTrue(is_safe_to_send(result))
        self.assertIn("/verify/sandbox?", requests[0].full_url)
        self.assertIn("email=safe-to-send%40example.com", requests[0].full_url)

    def test_bulk_upload_uses_csv_multipart_and_token_header(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return Response(b'{"success":true,"id":"job-1"}')

        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory, "emails.csv")
            csv_path.write_text("email\nstudio@example.com\n", encoding="utf-8")
            result = QuickEmailVerification("secret", opener=open_request).submit_bulk(csv_path)

        request = requests[0]
        self.assertEqual(result["id"], "job-1")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "token secret")
        self.assertIn(b'name="upload"; filename="emails.csv"', request.data)
        self.assertIn(b"studio@example.com", request.data)

    def test_wait_for_bulk_stops_on_completed_status(self):
        client = QuickEmailVerification("secret")
        statuses = iter([{"status": "running"}, {"status": "completed", "id": "job-1"}])
        client.bulk_status = lambda job_id: next(statuses)

        result = client.wait_for_bulk("job-1", poll_interval=0, timeout=1)

        self.assertEqual(result["status"], "completed")

    def test_download_rejects_untrusted_report_host(self):
        client = QuickEmailVerification("secret")
        with self.assertRaisesRegex(QEVError, "outside QuickEmailVerification"):
            client.download_report("https://example.com/report.csv", "report.csv")

    def test_http_error_preserves_status_code(self):
        def open_request(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 402, "Payment required", {},
                io.BytesIO(b'{"message":"Low credit"}'),
            )

        with self.assertRaises(QEVError) as raised:
            QuickEmailVerification("secret", opener=open_request).verify("a@example.com")
        self.assertEqual(raised.exception.status_code, 402)


if __name__ == "__main__":
    unittest.main()
