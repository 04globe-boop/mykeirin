"""Read-only probe for the collector's private storage credential."""

from datetime import datetime, timedelta, timezone
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO = "04globe-boop/mykeirin2"
API = f"https://api.github.com/repos/{REPO}"
BUNDLE_TAG = "research-model-v2"
UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def _read_json(url, token):
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "mykeirin-credential-health",
    })
    with OPENER.open(request, timeout=10) as response:
        value = json.loads(response.read(4 * 1024 * 1024))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def check(token, expiry, now):
    if not token:
        return "SECRET_MISSING", None
    try:
        metadata = _read_json(API, token)
        if metadata.get("full_name") != REPO or metadata.get("private") is not True:
            return "UNEXPECTED_REPOSITORY", None
        release = _read_json(f"{API}/releases/tags/{BUNDLE_TAG}", token)
        if release.get("tag_name") != BUNDLE_TAG:
            return "UNEXPECTED_RELEASE", None
    except HTTPError as error:
        code = error.code
        throttled = code == 429 or (code == 403 and (
            error.headers.get("X-RateLimit-Remaining") == "0" or error.headers.get("Retry-After") is not None
        ))
        error.close()
        if throttled or code >= 500:
            return "REMOTE_UNAVAILABLE", code
        if code == 401:
            return "AUTH_REJECTED", code
        if code in (403, 404):
            return "ACCESS_DENIED", code
        return "UNEXPECTED_HTTP_RESPONSE", code
    except (URLError, TimeoutError, OSError):
        return "REMOTE_UNAVAILABLE", None
    except (ValueError, UnicodeError):
        return "UNEXPECTED_HTTP_RESPONSE", None

    if not expiry:
        return "EXPIRY_UNKNOWN", None
    try:
        if UTC_STAMP.fullmatch(expiry) is None:
            raise ValueError("Expected a UTC timestamp")
        expires_at = datetime.fromisoformat(expiry)
    except ValueError:
        return "INVALID_EXPIRY", None
    remaining = expires_at - now
    if remaining <= timedelta(days=7):
        return "ROTATE_TOKEN", None
    if remaining <= timedelta(days=14):
        return "EXPIRY_NOTICE", None
    return "READ_OK", None


MESSAGES = {
    "SECRET_MISSING": ("error", "DATA_REPO_TOKEN was not supplied to this job."),
    "AUTH_REJECTED": ("error", "GitHub rejected the credential."),
    "ACCESS_DENIED": ("error", "The repository or release is unavailable or access was denied. Check the target, tag and permissions."),
    "UNEXPECTED_REPOSITORY": ("error", "Destination repository identity or privacy check failed."),
    "UNEXPECTED_RELEASE": ("error", "The expected release was not returned."),
    "UNEXPECTED_HTTP_RESPONSE": ("error", "GitHub returned an unexpected response."),
    "REMOTE_UNAVAILABLE": ("error", "The read check could not complete. Check GitHub status and retry."),
    "INVALID_EXPIRY": ("error", "Set DATA_REPO_TOKEN_EXPIRES_UTC in YYYY-MM-DDTHH:MM:SSZ format."),
    "EXPIRY_UNKNOWN": ("warning", "Set DATA_REPO_TOKEN_EXPIRES_UTC to enable expiry alerts."),
    "EXPIRY_NOTICE": ("warning", "The recorded expiry is within 14 days. Plan the next rotation."),
    "ROTATE_TOKEN": ("error", "The recorded expiry is within 7 days or has passed. Verify and rotate."),
    "READ_OK": ("notice", "Metadata and pinned release reads succeeded."),
}


def main(*, now=None):
    state, status = check(
        os.environ.get("DATA_REPO_TOKEN", ""),
        os.environ.get("DATA_REPO_TOKEN_EXPIRES_UTC", ""),
        now or datetime.now(timezone.utc),
    )
    level, message = MESSAGES[state]
    suffix = f" HTTP {status}." if status is not None else ""
    print(f"::{level}::{state}. {message}{suffix}")
    return 1 if level == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
