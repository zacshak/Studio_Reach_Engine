import csv
import tempfile
import unittest
from pathlib import Path

from Email_Verifier.verify_bulk import email_column, verify_csv


class FakeVerifier:
    def __init__(self):
        self.calls = []

    def verify(self, email):
        self.calls.append(email)
        result = "invalid" if email.startswith("dead") else "valid"
        return {"email": email, "result": result, "reason": "r", "accept_all": False, "source": "smtp"}


class VerifyCsvTest(unittest.TestCase):
    def test_writes_results_and_checks_each_address_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp, "in.csv"), Path(tmp, "out.csv")
            source.write_text('name,Email\nA,ok@x.test\nB,dead@x.test\nC,"OK@x.test, other@x.test"\nD,\n',
                              encoding="utf-8")
            verifier = FakeVerifier()
            summary = verify_csv(source, output, verifier)
            self.assertEqual(verifier.calls, ["ok@x.test", "dead@x.test"])
            self.assertEqual(summary["results"], {"valid": 2, "invalid": 2})
            rows = list(csv.DictReader(output.open(encoding="utf-8")))
            self.assertEqual([r["result"] for r in rows], ["valid", "invalid", "valid", "invalid"])
            self.assertEqual(rows[3]["reason"], "empty")
            self.assertEqual(rows[0]["name"], "A")

    def test_needs_an_email_column(self):
        self.assertEqual(email_column(["Name", "E-mail"]), "E-mail")
        with self.assertRaises(ValueError):
            email_column(["name", "website"])


if __name__ == "__main__":
    unittest.main()
