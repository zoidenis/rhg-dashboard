"""Email through Microsoft Graph: never contacts Microsoft; every HTTP call is a stand-in."""
import base64
import contextlib
import email
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

import graph_mail
import notify
import smtp_config

TENANT, CLIENT, SENDER = "tenant-1", "client-1", "pulse@rhg.al"


def make_pem(days=365):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RHG Pulse mail test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM)
    return pem, cert


def response(status, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {}
    return r


def b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class GraphMail(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        pem, self.cert = make_pem()
        self.cert_path = self.dir / "pulse-graph.pem"
        self.cert_path.write_bytes(pem)
        for name, value in (("MAIL_TRANSPORT", ""), ("GRAPH_TENANT_ID", TENANT), ("GRAPH_CLIENT_ID", CLIENT),
                            ("GRAPH_CERT_PATH", str(self.cert_path)), ("GRAPH_CERT_PASSWORD", "")):
            self.stack.enter_context(patch.object(graph_mail.config, name, value))
        graph_mail._token.update(value="", expires=0.0)
        graph_mail._cred.update(path="", stamp=None)
        self.addCleanup(graph_mail._token.update, value="", expires=0.0)
        self.post = self.stack.enter_context(patch.object(graph_mail.requests, "post",
                                                          return_value=response(200, {"access_token": "tok",
                                                                                      "expires_in": 3599})))
        self.request = self.stack.enter_context(patch.object(graph_mail.requests, "request"))
        self.put = self.stack.enter_context(patch.object(graph_mail.requests, "put", return_value=response(201)))

    def message(self, attachments=(), inline=()):
        msg = email.message.EmailMessage()
        msg["From"], msg["To"], msg["Cc"], msg["Subject"] = SENDER, "a@rhg.al, b@rhg.al", "c@rhg.al", "Fatura pa PO"
        msg["Reply-To"] = "geri@rhg.al"
        msg.set_content("text")
        msg.add_alternative("<p>html <img src='cid:logo'></p>", subtype="html")
        for cid, data in inline:
            msg.get_body(("html",)).add_related(data, maintype="image", subtype="png", cid=f"<{cid}>",
                                                 disposition="inline")
        for name, data in attachments:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        return msg

    # ---- which transport

    def test_graph_is_chosen_once_tenant_and_app_are_set(self):
        self.assertTrue(graph_mail.active())
        with patch.object(graph_mail.config, "MAIL_TRANSPORT", "smtp"):
            self.assertFalse(graph_mail.active())
        with patch.object(graph_mail.config, "GRAPH_CLIENT_ID", ""):
            self.assertFalse(graph_mail.active())
            with patch.object(graph_mail.config, "MAIL_TRANSPORT", "graph"):
                self.assertTrue(graph_mail.active())
                self.assertFalse(graph_mail.ready())
                self.assertIn("GRAPH_CLIENT_ID", graph_mail.status()["error"])

    def test_ready_needs_the_from_mailbox_not_a_password(self):
        values = {"host": "", "port": 587, "security": "starttls", "user": "", "password": "", "from": SENDER,
                  "public_url": "https://dashboard.rhg.al"}
        self.assertTrue(smtp_config.ready(values))
        self.assertFalse(smtp_config.ready(dict(values, **{"from": ""})))
        public = smtp_config._public(values)
        self.assertEqual(public["transport"], "graph")
        self.assertEqual(public["graph"]["certificate_expires"], self.cert.not_valid_after_utc.date().isoformat())
        self.assertNotIn("password", public)

    def test_missing_or_broken_certificate_is_reported(self):
        self.cert_path.write_text("not a certificate")
        self.assertFalse(graph_mail.ready())
        self.assertIn("certificate could not be read", graph_mail.status()["error"])
        with patch.object(graph_mail.config, "GRAPH_CERT_PATH", str(self.dir / "missing.pem")):
            self.assertIn("was not found", graph_mail.status()["error"])
            with self.assertRaises(RuntimeError):
                graph_mail.send(self.message(), SENDER)
        self.request.assert_not_called()

    def test_expired_certificate_is_an_error(self):
        pem, _cert = make_pem(days=-1)
        self.cert_path.write_bytes(pem)
        self.assertIn("expired", graph_mail.status()["error"])

    # ---- sign-in

    def test_token_request_is_signed_with_the_certificate_and_cached(self):
        self.request.return_value = response(202)
        graph_mail.send(self.message(), SENDER)
        graph_mail.send(self.message(), SENDER)
        self.assertEqual(self.post.call_count, 1)                      # the token is reused
        url = self.post.call_args.args[0]
        data = self.post.call_args.kwargs["data"]
        self.assertEqual(url, f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
        self.assertEqual(data["grant_type"], "client_credentials")
        self.assertEqual(data["scope"], "https://graph.microsoft.com/.default")
        self.assertNotIn("client_secret", data)
        head, claims, signature = data["client_assertion"].split(".")
        header = json.loads(b64url_decode(head))
        body = json.loads(b64url_decode(claims))
        der = self.cert.public_bytes(serialization.Encoding.DER)
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(b64url_decode(header["x5t"]), hashlib.sha1(der).digest())  # noqa: S324
        self.assertEqual(body["aud"], url)
        self.assertEqual((body["iss"], body["sub"]), (CLIENT, CLIENT))
        self.assertLessEqual(body["exp"] - body["iat"], 600)
        self.cert.public_key().verify(b64url_decode(signature), f"{head}.{claims}".encode(),
                                      padding.PKCS1v15(), hashes.SHA256())

    def test_sign_in_failure_names_the_reason(self):
        self.post.return_value = response(401, {"error": "invalid_client",
                                                "error_description": "AADSTS700027: Client assertion failed.\r\nTrace"})
        with self.assertRaisesRegex(RuntimeError, "AADSTS700027"):
            graph_mail.send(self.message(), SENDER)

    # ---- sending

    def test_small_message_goes_as_mime_in_one_request(self):
        self.request.return_value = response(202)
        graph_mail.send(self.message(inline=[("logo", b"\x89PNG small")]), SENDER)
        method, url = self.request.call_args.args
        kwargs = self.request.call_args.kwargs
        self.assertEqual((method, url), ("POST", "https://graph.microsoft.com/v1.0/users/pulse@rhg.al/sendMail"))
        self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        sent = email.message_from_bytes(base64.b64decode(kwargs["data"]), policy=policy.default)
        self.assertEqual(sent["Subject"], "Fatura pa PO")
        self.assertEqual(sent["Cc"], "c@rhg.al")

    def test_mime_lines_end_in_crlf(self):
        # bare LF made Exchange show "=" in place of letters in quoted-printable bodies (pulse@rhg.al, 19 Nov 2026)
        self.request.return_value = response(202)
        msg = self.message()
        msg.get_body(("html",)).set_content("<p>Përshëndetje " + "Executive Action Center " * 20 + "</p>", subtype="html")
        graph_mail.send(msg, SENDER)
        raw = base64.b64decode(self.request.call_args.kwargs["data"])
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"=\r\n", raw)                                     # soft breaks are CRLF
        sent = email.message_from_bytes(raw, policy=policy.default)
        self.assertIn("Executive Action Center " * 20, sent.get_body(("html",)).get_content())

    def test_refusal_explains_missing_mailbox_rights(self):
        self.request.return_value = response(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        with self.assertRaisesRegex(RuntimeError, "ErrorAccessDenied.*Exchange role assignment"):
            graph_mail.send(self.message(), SENDER)

    def test_expired_token_is_renewed_once(self):
        self.request.side_effect = [response(401), response(202)]
        graph_mail.send(self.message(), SENDER)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.request.call_count, 2)

    def test_large_message_uses_a_draft_and_upload_sessions(self):
        big = bytes(range(256)) * 20_000                                  # 5.1 MB, past the one-request limit
        small = b"%PDF small"
        calls = []

        def graph(method, url, headers=None, **kwargs):
            calls.append((method, url.rsplit("/v1.0", 1)[-1], kwargs.get("json")))
            if url.endswith("/messages"):
                return response(201, {"id": "draft/1"})
            if url.endswith("/createUploadSession"):
                return response(201, {"uploadUrl": "https://upload.example/session"})
            if url.endswith("/attachments"):
                return response(201)
            return response(202)

        self.request.side_effect = graph
        graph_mail.send(self.message(attachments=[("big.pdf", big), ("small.pdf", small)],
                                     inline=[("logo", b"\x89PNG")]), SENDER)
        draft = calls[0][2]
        self.assertEqual(draft["subject"], "Fatura pa PO")
        self.assertEqual(draft["body"]["contentType"], "HTML")
        self.assertEqual([r["emailAddress"]["address"] for r in draft["toRecipients"]], ["a@rhg.al", "b@rhg.al"])
        self.assertEqual(draft["replyTo"][0]["emailAddress"]["address"], "geri@rhg.al")
        steps = [(m, u.split("/messages/")[-1] if "/messages/" in u else u) for m, u, _ in calls]
        self.assertEqual(steps, [("POST", "/users/pulse@rhg.al/messages"),
                                 ("POST", "draft%2F1/attachments"),
                                 ("POST", "draft%2F1/attachments/createUploadSession"),
                                 ("POST", "draft%2F1/attachments"),
                                 ("POST", "draft%2F1/send")])
        logo = calls[1][2]
        self.assertEqual((logo["isInline"], logo["contentId"]), (True, "logo"))
        self.assertEqual(calls[2][2]["AttachmentItem"]["size"], len(big))
        uploaded = b"".join(c.kwargs["data"] for c in self.put.call_args_list)
        self.assertEqual(uploaded, big)
        for c in self.put.call_args_list:
            self.assertNotIn("Authorization", c.kwargs["headers"])
            self.assertEqual(len(c.kwargs["data"]) % (320 * 1024) == 0 or c is self.put.call_args_list[-1], True)
        self.assertTrue(self.put.call_args_list[-1].kwargs["headers"]["Content-Range"]
                        .endswith(f"-{len(big) - 1}/{len(big)}"))

    def test_failed_large_message_deletes_its_draft(self):
        big = b"x" * 4_000_000
        seen = []

        def graph(method, url, headers=None, **kwargs):
            seen.append(method)
            if url.endswith("/messages"):
                return response(201, {"id": "d1"})
            if url.endswith("/createUploadSession"):
                return response(400, {"error": {"code": "ErrorInvalidRequest", "message": "bad"}})
            return response(204)

        self.request.side_effect = graph
        with self.assertRaisesRegex(RuntimeError, "ErrorInvalidRequest"):
            graph_mail.send(self.message(attachments=[("big.pdf", big)]), SENDER)
        self.assertEqual(seen[-1], "DELETE")

    # ---- the towers' one sending function

    def test_send_mail_hands_the_message_to_graph(self):
        values = {"host": "", "port": 587, "security": "starttls", "user": "", "password": "", "from": SENDER,
                  "public_url": "https://dashboard.rhg.al"}
        with patch.object(smtp_config, "current", return_value=values), \
                patch.object(graph_mail, "send") as send, patch.object(notify.smtplib, "SMTP") as smtp:
            notify.send_mail(["a@rhg.al"], [], "Test", "<p>x</p>", "x")
        smtp.assert_not_called()
        msg, sender = send.call_args.args
        self.assertEqual((msg["Subject"], msg["From"], sender), ("Test", SENDER, SENDER))

    def test_send_mail_says_why_graph_is_not_ready(self):
        values = {"host": "", "port": 587, "security": "starttls", "user": "", "password": "", "from": SENDER,
                  "public_url": "https://dashboard.rhg.al"}
        with patch.object(smtp_config, "current", return_value=values), \
                patch.object(graph_mail.config, "GRAPH_CERT_PATH", ""):
            with self.assertRaisesRegex(RuntimeError, "Microsoft Graph email is not ready: GRAPH_CERT_PATH"):
                notify.send_mail(["a@rhg.al"], [], "Test", "<p>x</p>", "x")


if __name__ == "__main__":
    unittest.main()
