import io
import json
import unittest
import urllib.error

from Email_Verifier.abstract_client import AbstractEmailReputation, AbstractError, map_reputation


def reputation(status, detail, *, catchall=False, disposable=False, format_valid=True,
               smtp_valid=False, mx_valid=True, address="someone@example.com"):
    """Shape recorded from live Email Reputation responses (unused sections trimmed)."""
    return {
        "email_address": address,
        "suggested_correction": None,
        "email_deliverability": {
            "status": status, "status_detail": detail, "is_format_valid": format_valid,
            "is_smtp_valid": smtp_valid, "is_mx_valid": mx_valid,
            "mx_records": ["mx.example.com"] if mx_valid else None,
        },
        "email_quality": {
            "score": 0.0, "is_free_email": False, "is_username_suspicious": False,
            "is_disposable": disposable, "is_catchall": catchall, "is_subaddress": False,
            "is_role": False, "is_dmarc_enforced": False, "is_spf_strict": False,
            "minimum_age": None,
        },
        "email_risk": {"address_risk_status": "high", "domain_risk_status": "low"},
    }


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.headers = {}


def http_error(code, message, error_code):
    body = json.dumps({"error": {"message": message, "code": error_code, "details": None}})
    return urllib.error.HTTPError("https://x", code, message, {}, io.BytesIO(body.encode()))


class MappingTest(unittest.TestCase):
    def check(self, data, result, accept_all=False, safe=None):
        mapped = map_reputation("a@b.dev", data)
        self.assertEqual((mapped["result"], mapped["accept_all"], mapped["source"], mapped["email"]),
                         (result, accept_all, "abstract", "a@b.dev"))
        self.assertEqual(mapped["safe_to_send"], result == "valid" if safe is None else safe)
        return mapped

    def test_live_nonexistent_gmail_mailbox_is_invalid(self):
        mapped = self.check(reputation("undeliverable", "invalid_mailbox"), "invalid")
        self.assertEqual(mapped["reason"], "undeliverable:invalid_mailbox")

    def test_live_nonexistent_domain_is_invalid(self):
        self.check(reputation("undeliverable", "dns_record_not_found", mx_valid=False), "invalid")

    def test_bad_format_is_invalid(self):
        self.check(reputation("undeliverable", "invalid_format", format_valid=False), "invalid")
        self.check(reputation("unknown", None, format_valid=False), "invalid")

    def test_deliverable_is_valid(self):
        self.check(reputation("deliverable", "valid_email", smtp_valid=True), "valid")
        self.check(reputation("deliverable", "high_traffic_email", smtp_valid=True), "valid")

    def test_disposable_is_valid_but_not_safe(self):
        self.check(reputation("deliverable", "valid_email", disposable=True), "valid", safe=False)

    def test_catch_all_is_valid_with_accept_all(self):
        self.check(reputation("unknown", None, catchall=True), "valid", accept_all=True)
        self.check(reputation("deliverable", "valid_email", catchall=True), "valid", accept_all=True)
        # "No such mailbox" on a domain that accepts everything is a contradiction: unknown.
        self.check(reputation("undeliverable", "invalid_mailbox", catchall=True), "unknown",
                   accept_all=True)

    def test_uncertain_answers_stay_unknown(self):
        self.check(reputation("unknown", None), "unknown")
        self.check(reputation("undeliverable", "full_mailbox"), "unknown")
        self.check(reputation("undeliverable", "unavailable_server"), "unknown")
        self.check(reputation("risky", "whatever"), "unknown")
        self.check({"email_deliverability": {}}, "unknown")


class ClientTest(unittest.TestCase):
    def test_request_uses_key_param_and_maps_result(self):
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return Response(reputation("undeliverable", "invalid_mailbox"))

        client = AbstractEmailReputation("secret", opener=open_request)
        result = client.verify(" a@b.dev ")
        url = requests[0].full_url
        self.assertTrue(url.startswith("https://emailreputation.abstractapi.com/v1/?"))
        self.assertIn("api_key=secret", url)
        self.assertIn("email=a%40b.dev", url)
        self.assertEqual((result["email"], result["result"]), ("a@b.dev", "invalid"))

    def test_quota_and_bad_key_raise_with_status(self):
        for code, msg, err in ((401, "Invalid API key provided.", "unauthorized"),
                               (422, "Quota reached", "quota_reached"),
                               (403, "Insufficient credits", "forbidden"),
                               (500, "boom", "server_error")):
            def open_request(request, timeout, code=code, msg=msg, err=err):
                raise http_error(code, msg, err)

            with self.assertRaises(AbstractError) as ctx:
                AbstractEmailReputation("k", opener=open_request).verify("a@b.dev")
            self.assertEqual(ctx.exception.status_code, code)
            self.assertIn(msg, str(ctx.exception))

    def test_rate_limit_is_retried_once(self):
        calls, sleeps = [], []

        def open_request(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise http_error(429, "Too many requests", "too_many_requests")
            return Response(reputation("deliverable", "valid_email"))

        client = AbstractEmailReputation("k", opener=open_request, sleep=sleeps.append,
                                         clock=lambda: 0.0)
        self.assertEqual(client.verify("a@b.dev")["result"], "valid")
        self.assertEqual((len(calls), len(sleeps)), (2, 1))

        def always_429(request, timeout):
            raise http_error(429, "Too many requests", "too_many_requests")

        with self.assertRaises(AbstractError) as ctx:
            AbstractEmailReputation("k", opener=always_429, sleep=lambda s: None).verify("a@b.dev")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_error_body_network_failure_and_garbage_raise(self):
        for opener in (lambda r, timeout: Response({"error": {"message": "nope"}}),
                       lambda r, timeout: Response(b"not json"),
                       lambda r, timeout: Response([1, 2]),
                       lambda r, timeout: Response({"email_address": "a@b.dev"})):
            with self.assertRaises(AbstractError):
                AbstractEmailReputation("k", opener=opener).verify("a@b.dev")

        def offline(request, timeout):
            raise urllib.error.URLError("offline")

        with self.assertRaises(AbstractError) as ctx:
            AbstractEmailReputation("k", opener=offline).verify("a@b.dev")
        self.assertIsNone(ctx.exception.status_code)

    def test_from_env_requires_key(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ABSTRACT_API_KEY": ""}):
            with self.assertRaises(AbstractError):
                AbstractEmailReputation.from_env()
        with mock.patch.dict(os.environ, {"ABSTRACT_API_KEY": "k"}):
            self.assertEqual(AbstractEmailReputation.from_env().api_key, "k")


if __name__ == "__main__":
    unittest.main()
