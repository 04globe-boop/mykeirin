import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
import credential_health as health

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


class Response(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class CredentialHealthTest(unittest.TestCase):
    def test_auth_error_uses_fixed_message_and_status_only(self):
        response = HTTPError("https://api.github.com", 401, "sensitive body", {}, io.BytesIO(b"sensitive body"))
        with patch.object(health, "OPENER") as opener:
            opener.open.side_effect = response
            with patch.dict("os.environ", {"DATA_REPO_TOKEN": "example-secret"}, clear=True):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(health.main(now=NOW), 1)
            self.assertIn("AUTH_REJECTED", stream.getvalue())
            self.assertNotIn("sensitive", stream.getvalue())
            self.assertNotIn("example-secret", stream.getvalue())

    def test_distinguishes_invalid_scope_rate_limit_and_connectivity(self):
        for error, expected in (
            (HTTPError("", 404, "", {}, None), "ACCESS_DENIED"),
            (HTTPError("", 403, "", {}, None), "ACCESS_DENIED"),
            (HTTPError("", 403, "", {"X-RateLimit-Remaining": "0"}, None), "REMOTE_UNAVAILABLE"),
            (HTTPError("", 429, "", {}, None), "REMOTE_UNAVAILABLE"),
            (URLError("untrusted message"), "REMOTE_UNAVAILABLE"),
        ):
            with self.subTest(expected=expected), patch.object(health, "OPENER") as opener:
                opener.open.side_effect = error
                self.assertEqual(health.check("dummy", "", NOW)[0], expected)

    def test_valid_access_and_expiry_boundaries(self):
        for expiry, expected in (
            ("2026-12-06T00:00:00Z", "READ_OK"),
            ("2026-12-05T00:00:00Z", "EXPIRY_NOTICE"),
            ("2026-11-28T00:00:00Z", "ROTATE_TOKEN"),
            ("", "EXPIRY_UNKNOWN"),
            ("2026-12-06", "INVALID_EXPIRY"),
        ):
            with self.subTest(expiry=expiry), patch.object(health, "OPENER") as opener:
                opener.open.side_effect = [Response({"full_name": "04globe-boop/mykeirin2", "private": True}),
                                           Response({"tag_name": "research-model-v2"})]
                self.assertEqual(health.check("dummy", expiry, NOW)[0], expected)
                self.assertEqual([c.args[0].get_method() for c in opener.open.call_args_list], ["GET", "GET"])

    def test_missing_secret_never_calls_api(self):
        with patch.object(health, "OPENER") as opener:
            self.assertEqual(health.check("", "", NOW)[0], "SECRET_MISSING")
            opener.open.assert_not_called()

    def test_wrong_repository_visibility_fails_closed(self):
        with patch.object(health, "OPENER") as opener:
            opener.open.return_value = Response({"full_name": "04globe-boop/mykeirin2", "private": False})
            self.assertEqual(health.check("dummy", "", NOW)[0], "UNEXPECTED_REPOSITORY")
            opener.open.assert_called_once()

    def test_redirect_is_not_followed(self):
        handler = health.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://external.invalid"))


if __name__ == "__main__":
    unittest.main()
