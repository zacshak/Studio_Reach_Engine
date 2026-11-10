import unittest

import dns.resolver

from Email_Verifier.abstract_client import AbstractError
from Email_Verifier.client import QEVError
from Email_Verifier.smtp_check import SMTPVerifier, classify_rcpt
from Email_Verifier.verifier import EmailVerifier


class MX:
    def __init__(self, preference, exchange):
        self.preference, self.exchange = preference, exchange


class Resolver:
    """records: {(domain, rtype): list | exception class}"""

    def __init__(self, records):
        self.records, self.lifetime, self.calls = records, None, []

    def resolve(self, domain, rtype):
        self.calls.append((domain, rtype))
        value = self.records.get((domain, rtype), dns.resolver.NoAnswer)
        if isinstance(value, type) and issubclass(value, Exception):
            raise value()
        return value


class Server:
    """Fake SMTP server. mailboxes: set of accepted addresses; reply(rcpt) overrides."""

    def __init__(self, mailboxes=(), *, catch_all=False, rcpt=None, mail=(250, b"ok"),
                 connect=(220, b"hi"), down=()):
        self.mailboxes, self.catch_all, self.rcpt_reply = set(mailboxes), catch_all, rcpt
        self.mail_reply, self.connect_reply, self.down = mail, connect, set(down)
        self.log, self.data_sent = [], False

    def factory(self):
        server = self

        class Session:
            def connect(self, host, port):
                server.log.append(("connect", host, port))
                if host in server.down:
                    raise ConnectionRefusedError("refused")
                return server.connect_reply

            def ehlo(self, name):
                return 250, b"hello"

            helo = ehlo

            def has_extn(self, name):
                return False

            def mail(self, sender):
                server.log.append(("mail", sender))
                return server.mail_reply

            def rcpt(self, addr):
                server.log.append(("rcpt", addr))
                if server.rcpt_reply:
                    return server.rcpt_reply(addr)
                if server.catch_all or addr in server.mailboxes:
                    return 250, b"2.1.5 OK"
                return 550, b"5.1.1 The email account that you tried to reach does not exist."

            def rset(self):
                return 250, b"ok"

            def data(self, *a):
                server.data_sent = True

            sendmail = send_message = data

            def quit(self):
                server.log.append(("quit",))

            close = quit

        return Session()


GOOD_DNS = {("studio.dev", "MX"): [MX(20, "mx2.studio.dev."), MX(10, "mx1.studio.dev.")]}


def verifier(server, records=GOOD_DNS, **kw):
    return SMTPVerifier(helo="probe.test", mail_from="me@sender.test", resolver=Resolver(records),
                        smtp_factory=server.factory, **kw)


class ClassifyTest(unittest.TestCase):
    def test_only_explicit_no_mailbox_answers_are_invalid(self):
        cases = [
            (250, "2.1.5 OK", "valid"),
            (550, "5.1.1 <x@y> User unknown", "invalid"),
            (550, "5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found", "invalid"),
            (550, "No such user here", "invalid"),
            (550, "Requested action not taken: mailbox unavailable", "invalid"),
            (552, "5.2.2 Mailbox full", "unknown"),
            (550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)", "invalid"),
            (550, "5.4.1 Relay Access Denied", "unknown"),
            (554, "RCPT (letsgo@x.com) dosn't exist", "invalid"),
            (554, "5.7.1 <a@b.com>: Relay access denied", "unknown"),
            (550, "5.7.25 Forward-confirmed reverse DNS failed", "unknown"),
            (550, "5.7.1 Service unavailable, client host blocked using Spamhaus", "unknown"),
            (550, "5.7.606 Access denied, banned sending IP", "unknown"),
            (554, "5.7.1 Recipient address rejected: Access denied", "unknown"),
            (550, "Mailbox unavailable: blocked by policy", "unknown"),
            (450, "4.2.0 Greylisted, please try again", "unknown"),
            (451, "4.7.1 Try again later", "unknown"),
            (421, "Too many connections", "unknown"),
            (None, "", "unknown"),
        ]
        for code, msg, want in cases:
            with self.subTest(code=code, msg=msg):
                self.assertEqual(classify_rcpt(code, msg), want)


class SMTPVerifierTest(unittest.TestCase):
    def test_existing_mailbox_is_valid_and_no_mail_is_sent(self):
        server = Server({"hello@studio.dev"})
        result = verifier(server).verify("hello@studio.dev")
        self.assertEqual((result["result"], result["reason"]), ("valid", "mailbox_exists"))
        self.assertTrue(result["safe_to_send"])
        self.assertEqual(result["mx"], "mx1.studio.dev")  # lowest preference first
        self.assertEqual(server.log[0], ("connect", "mx1.studio.dev", 25))
        self.assertIn(("mail", "me@sender.test"), server.log)
        self.assertFalse(server.data_sent)
        self.assertEqual(server.log[-1], ("quit",))

    def test_missing_mailbox_is_invalid(self):
        result = verifier(Server({"hello@studio.dev"})).verify("nobody@studio.dev")
        self.assertEqual(result["result"], "invalid")
        self.assertFalse(result["safe_to_send"])

    def test_catch_all_domain_is_unknown_not_valid(self):
        result = verifier(Server(catch_all=True)).verify("hello@studio.dev")
        self.assertEqual((result["result"], result["reason"]), ("unknown", "accept_all"))
        self.assertTrue(result["accept_all"])

    def test_inconclusive_catch_all_probe_is_unknown(self):
        server = Server(rcpt=lambda a: (250, b"ok") if a.startswith("hello@") else (451, b"later"))
        result = verifier(server).verify("hello@studio.dev")
        self.assertEqual(result["result"], "unknown")

    def test_server_refusing_random_address_with_bare_5xx_still_proves_ours(self):
        server = Server(rcpt=lambda a: (250, b"ok") if a.startswith("hello@") else (550, b"nope"))
        self.assertEqual(verifier(server).verify("hello@studio.dev")["result"], "valid")

    def test_policy_block_and_greylisting_are_unknown(self):
        for reply in [(550, b"5.7.1 client host blocked using Spamhaus"), (450, b"4.2.0 greylisted")]:
            with self.subTest(reply=reply):
                result = verifier(Server(rcpt=lambda a, r=reply: r)).verify("hello@studio.dev")
                self.assertEqual(result["result"], "unknown")

    def test_rejected_sender_is_unknown(self):
        result = verifier(Server({"hello@studio.dev"}, mail=(550, b"5.7.1 SPF fail"))).verify(
            "hello@studio.dev")
        self.assertEqual(result["result"], "unknown")

    def test_falls_back_to_next_mx_when_first_is_down(self):
        server = Server({"hello@studio.dev"}, down={"mx1.studio.dev"})
        result = verifier(server).verify("hello@studio.dev")
        self.assertEqual((result["result"], result["mx"]), ("valid", "mx2.studio.dev"))

    def test_unexpected_client_error_is_unknown_not_a_crash(self):
        server = Server({"hello@studio.dev"})
        server.mail_reply = None  # unpacking None raises TypeError inside the session
        self.assertEqual(verifier(server).verify("hello@studio.dev")["result"], "unknown")

    def test_all_mx_down_is_unknown(self):
        server = Server(down={"mx1.studio.dev", "mx2.studio.dev"})
        self.assertEqual(verifier(server).verify("hello@studio.dev")["result"], "unknown")

    def test_dns_outcomes(self):
        cases = [
            ({("gone.dev", "MX"): dns.resolver.NXDOMAIN}, "invalid", "domain_does_not_exist"),
            ({("null.dev", "MX"): [MX(0, ".")]}, "invalid", "null_mx"),
            ({("nomx.dev", "MX"): dns.resolver.NoAnswer}, "invalid", "no_mail_server"),
            ({("slow.dev", "MX"): dns.resolver.LifetimeTimeout}, "unknown", "dns_error"),
        ]
        for records, want, reason in cases:
            domain = next(iter(records))[0]
            with self.subTest(domain=domain):
                result = verifier(Server(), records).verify(f"a@{domain}")
                self.assertEqual((result["result"], result["reason"]), (want, reason))

    def test_implicit_mx_uses_a_record(self):
        records = {("bare.dev", "MX"): dns.resolver.NoAnswer, ("bare.dev", "A"): ["1.2.3.4"]}
        server = Server({"a@bare.dev"})
        result = verifier(server, records).verify("a@bare.dev")
        self.assertEqual(result["result"], "valid")
        self.assertEqual(server.log[0], ("connect", "bare.dev", 25))

    def test_bad_syntax_never_touches_network(self):
        resolver_records = {}
        v = verifier(Server(), resolver_records)
        for bad in ["", "no-at.dev", "a@b", "a@@b.dev", "a b@c.dev", "a@-x.dev"]:
            with self.subTest(bad=bad):
                self.assertEqual(v.verify(bad)["result"], "invalid")
        self.assertEqual(v._resolver.calls, [])

    def test_role_addresses_are_not_rejected(self):
        # Studios often publish contact@/info@ as their only address.
        server = Server({"info@studio.dev", "contact@studio.dev"})
        for email in ["info@studio.dev", "contact@studio.dev"]:
            self.assertEqual(verifier(server).verify(email)["result"], "valid")

    def test_domain_lookup_is_cached_per_run(self):
        v = verifier(Server({"a@studio.dev", "b@studio.dev"}))
        v.verify("a@studio.dev"), v.verify("b@studio.dev")
        self.assertEqual(v._resolver.calls, [("studio.dev", "MX")])


class StubSMTP:
    def __init__(self, result, **extra):
        self.result, self.extra = result, extra

    def verify(self, email):
        return {"email": email, "result": self.result, "reason": "r", "source": "smtp", **self.extra}


class StubQEV:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def verify(self, email):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {"email": email, "result": self.outcome}


class EmailVerifierTest(unittest.TestCase):
    def test_definitive_smtp_answer_never_spends_qev_credit(self):
        for outcome in ("valid", "invalid"):
            qev = StubQEV("valid")
            result = EmailVerifier(StubSMTP(outcome), qev, log=lambda *_: None).verify("a@b.dev")
            self.assertEqual((result["result"], qev.calls), (outcome, 0))

    def test_catch_all_domain_is_valid_without_spending_qev(self):
        qev = StubQEV("invalid")
        result = EmailVerifier(StubSMTP("unknown", accept_all=True), qev, log=lambda *_: None).verify("a@b.dev")
        self.assertEqual((result["result"], result["accept_all"], result["source"], qev.calls),
                         ("valid", True, "smtp", 0))

    def test_inconclusive_smtp_falls_back_to_qev(self):
        qev = StubQEV("invalid")
        result = EmailVerifier(StubSMTP("unknown"), qev, log=lambda *_: None).verify("a@b.dev")
        self.assertEqual((result["result"], result["source"], qev.calls), ("invalid", "qev", 1))

    def test_no_qev_or_qev_failure_leaves_unknown_and_stops_asking(self):
        self.assertEqual(EmailVerifier(StubSMTP("unknown"), None, log=lambda *_: None)
                         .verify("a@b.dev")["result"], "unknown")
        for error in (QEVError("quota", status_code=402), QEVError("down")):
            qev = StubQEV(error)
            chain = EmailVerifier(StubSMTP("unknown"), qev, log=lambda *_: None)
            self.assertEqual(chain.verify("a@b.dev")["result"], "unknown")
            self.assertEqual(chain.verify("c@b.dev")["result"], "unknown")
            self.assertEqual(qev.calls, 1)

    def chain(self, smtp="unknown", qev="valid", abstract="valid", **smtp_extra):
        qev = None if qev is None else StubQEV(qev)
        abstract = None if abstract is None else StubQEV(abstract)
        return (EmailVerifier(StubSMTP(smtp, **smtp_extra), qev, abstract, log=lambda *_: None),
                qev, abstract)

    def test_qev_answer_never_spends_abstract_credit(self):
        chain, qev, abstract = self.chain(qev="invalid")
        result = chain.verify("a@b.dev")
        self.assertEqual((result["result"], result["source"], qev.calls, abstract.calls),
                         ("invalid", "qev", 1, 0))

    def test_definitive_smtp_or_catch_all_calls_neither_fallback(self):
        for smtp, extra in (("valid", {}), ("invalid", {}), ("unknown", {"accept_all": True})):
            chain, qev, abstract = self.chain(smtp, qev="invalid", abstract="invalid", **extra)
            self.assertNotEqual(chain.verify("a@b.dev")["result"], "unknown")
            self.assertEqual((qev.calls, abstract.calls), (0, 0))

    def test_qev_failure_switches_to_abstract_for_the_rest_of_the_run(self):
        for error in (QEVError("quota", status_code=402), QEVError("down")):
            chain, qev, abstract = self.chain(qev=error, abstract="invalid")
            first, second = chain.verify("a@b.dev"), chain.verify("c@b.dev")
            self.assertEqual((first["result"], first["source"], first["smtp_reason"]),
                             ("invalid", "abstract", "r"))
            self.assertEqual(second["result"], "invalid")
            self.assertEqual((qev.calls, abstract.calls), (1, 2))

    def test_abstract_used_directly_when_qev_not_configured(self):
        chain, _, abstract = self.chain(qev=None, abstract="valid")
        self.assertEqual(chain.verify("a@b.dev")["result"], "valid")
        self.assertEqual(abstract.calls, 1)

    def test_both_fallbacks_exhausted_leave_unknown_and_stop_asking(self):
        for error in (AbstractError("quota", status_code=422), AbstractError("bad key", status_code=401),
                      AbstractError("offline")):
            chain, qev, abstract = self.chain(qev=QEVError("quota", status_code=402), abstract=error)
            results = [chain.verify(e) for e in ("a@b.dev", "c@b.dev", "d@b.dev")]
            self.assertEqual([r["result"] for r in results], ["unknown"] * 3)
            self.assertEqual({r["source"] for r in results}, {"smtp"})
            self.assertEqual((qev.calls, abstract.calls), (1, 1))


if __name__ == "__main__":
    unittest.main()
