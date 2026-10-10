import unittest

from cmkoffline2mail.config import MailConfig, SmtpConfig
from cmkoffline2mail.mailer import MailError, Mailer


def make_mailer(**mail_kwargs):
    mail = MailConfig(
        sender="checkmk@example.com",
        sender_name="Checkmk Monitoring",
        recipients=["it@example.com"],
        **mail_kwargs,
    )
    return Mailer(SmtpConfig(host="smtp.example.com"), mail, dry_run=True)


class BuildMessageTest(unittest.TestCase):
    def test_multipart_alternative(self):
        message = make_mailer().build_message("Betreff", ["it@example.com"], "Nur Text", "<p>HTML</p>")
        self.assertTrue(message.is_multipart())
        types = [part.get_content_type() for part in message.walk()]
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)
        self.assertEqual(message["From"], "Checkmk Monitoring <checkmk@example.com>")
        self.assertEqual(message["Subject"], "Betreff")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")

    def test_text_only(self):
        message = make_mailer().build_message("Betreff", ["it@example.com"], "Nur Text", None)
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIn("Nur Text", message.get_content())

    def test_html_only(self):
        message = make_mailer().build_message("Betreff", ["it@example.com"], None, "<p>HTML</p>")
        self.assertEqual(message.get_content_type(), "text/html")

    def test_headers(self):
        mailer = make_mailer(reply_to="servicedesk@example.com")
        message = mailer.build_message("B", ["a@example.com"], "t", None, cc=["chef@example.com"])
        self.assertEqual(message["Cc"], "chef@example.com")
        self.assertEqual(message["Reply-To"], "servicedesk@example.com")
        self.assertTrue(message["Message-ID"].endswith("example.com>"))

    def test_sender_without_name(self):
        mail = MailConfig(sender="checkmk@example.com", recipients=["it@example.com"])
        message = Mailer(SmtpConfig(host="h"), mail, dry_run=True).build_message("B", ["a@example.com"], "t", None)
        self.assertEqual(message["From"], "checkmk@example.com")

    def test_umlauts_survive(self):
        message = make_mailer().build_message("Grüße", ["it@example.com"], "Süd & Ost", None)
        self.assertIn("Süd & Ost", message.get_content())
        self.assertIn("Grüße", str(message["Subject"]))

    def test_empty_body_raises(self):
        with self.assertRaises(MailError):
            make_mailer().build_message("Betreff", ["it@example.com"], None, None)

    def test_send_without_recipient_raises(self):
        with self.assertRaises(MailError):
            make_mailer().send("Betreff", [], "Text", None)

    def test_dry_run_does_not_connect(self):
        mailer = make_mailer()
        mailer.connect = lambda: (_ for _ in ()).throw(AssertionError("darf nicht verbinden"))
        mailer.send("Betreff", ["it@example.com"], "Text", None)


class DeduplicationTest(unittest.TestCase):
    def test_envelope_deduplicates_recipients(self):
        sent = {}
        mailer = make_mailer()
        mailer.dry_run = False

        class FakeConnection:
            def send_message(self, message, from_addr, to_addrs):
                sent["to"] = to_addrs

        mailer.connect = lambda: FakeConnection()
        mailer.send("B", ["a@example.com", "b@example.com"], "Text", None, cc=["a@example.com"], bcc=["c@example.com"])
        self.assertEqual(sent["to"], ["a@example.com", "b@example.com", "c@example.com"])


if __name__ == "__main__":
    unittest.main()
