import unittest

from cmkoffline2mail.config import MailConfig, SmtpConfig
from cmkoffline2mail.mailer import MailError, Mailer


def make_mailer(**mail_kwargs):
    mail = MailConfig(
        sender="checkmk@firma.de",
        sender_name="Checkmk Monitoring",
        recipients=["it@firma.de"],
        **mail_kwargs,
    )
    return Mailer(SmtpConfig(host="smtp.firma.de"), mail, dry_run=True)


class BuildMessageTest(unittest.TestCase):
    def test_multipart_alternative(self):
        message = make_mailer().build_message("Betreff", ["it@firma.de"], "Nur Text", "<p>HTML</p>")
        self.assertTrue(message.is_multipart())
        types = [part.get_content_type() for part in message.walk()]
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)
        self.assertEqual(message["From"], "Checkmk Monitoring <checkmk@firma.de>")
        self.assertEqual(message["Subject"], "Betreff")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")

    def test_text_only(self):
        message = make_mailer().build_message("Betreff", ["it@firma.de"], "Nur Text", None)
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIn("Nur Text", message.get_content())

    def test_html_only(self):
        message = make_mailer().build_message("Betreff", ["it@firma.de"], None, "<p>HTML</p>")
        self.assertEqual(message.get_content_type(), "text/html")

    def test_headers(self):
        mailer = make_mailer(reply_to="servicedesk@firma.de")
        message = mailer.build_message("B", ["a@x.de"], "t", None, cc=["chef@firma.de"])
        self.assertEqual(message["Cc"], "chef@firma.de")
        self.assertEqual(message["Reply-To"], "servicedesk@firma.de")
        self.assertTrue(message["Message-ID"].endswith("firma.de>"))

    def test_sender_without_name(self):
        mail = MailConfig(sender="checkmk@firma.de", recipients=["it@firma.de"])
        message = Mailer(SmtpConfig(host="h"), mail, dry_run=True).build_message("B", ["a@x.de"], "t", None)
        self.assertEqual(message["From"], "checkmk@firma.de")

    def test_umlauts_survive(self):
        message = make_mailer().build_message("Grüße", ["it@firma.de"], "Süd & Ost", None)
        self.assertIn("Süd & Ost", message.get_content())
        self.assertIn("Grüße", str(message["Subject"]))

    def test_empty_body_raises(self):
        with self.assertRaises(MailError):
            make_mailer().build_message("Betreff", ["it@firma.de"], None, None)

    def test_send_without_recipient_raises(self):
        with self.assertRaises(MailError):
            make_mailer().send("Betreff", [], "Text", None)

    def test_dry_run_does_not_connect(self):
        mailer = make_mailer()
        mailer.connect = lambda: (_ for _ in ()).throw(AssertionError("darf nicht verbinden"))
        mailer.send("Betreff", ["it@firma.de"], "Text", None)


class DeduplicationTest(unittest.TestCase):
    def test_envelope_deduplicates_recipients(self):
        sent = {}
        mailer = make_mailer()
        mailer.dry_run = False

        class FakeConnection:
            def send_message(self, message, from_addr, to_addrs):
                sent["to"] = to_addrs

        mailer.connect = lambda: FakeConnection()
        mailer.send("B", ["a@x.de", "b@x.de"], "Text", None, cc=["a@x.de"], bcc=["c@x.de"])
        self.assertEqual(sent["to"], ["a@x.de", "b@x.de", "c@x.de"])


if __name__ == "__main__":
    unittest.main()
