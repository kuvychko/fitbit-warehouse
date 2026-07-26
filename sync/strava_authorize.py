"""One-time interactive Strava OAuth authorization for the sync poller.

    python -m sync.strava_authorize [--probe-only]

Opens a browser for consent, catches the redirect on http://localhost:8765/,
exchanges the code, and stores the token file at STRAVA_TOKEN_PATH (default
./secrets/strava_token.json — gitignored). With --probe-only, skips auth and
just exercises a refresh + a small read with the stored token.

DIVERGES from sync/authorize.py (Google) on purpose: Strava rotates the
refresh token on EVERY refresh, so the response must be written back after
every refresh, not only at initial authorization. Do not "simplify" this
module back onto the Google shape (which discards everything but the access
token) — the poller would break after its first refresh cycle. The write is
atomic and happens BEFORE the access token is used, so a crash mid-cycle can
never strand the poller with a dead (already-superseded) refresh token.

Stdlib only; .env in the current directory is read automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# OAuth endpoints stay on www.strava.com even after the 2027 API-base
# migration (which only moves the /api/v3 data endpoints); only STRAVA_API_BASE
# is configurable (see api_base()).
AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REDIRECT_URI = "http://localhost:8765/"
SCOPE = "activity:read_all"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def token_path() -> Path:
    return Path(os.environ.get("STRAVA_TOKEN_PATH", "./secrets/strava_token.json"))


def api_base() -> str:
    return os.environ.get("STRAVA_API_BASE", "https://www.strava.com/api/v3").rstrip("/")


def _write_token_atomic(path: Path, tokens: dict) -> None:
    """Write the token file atomically (temp in the same dir + os.replace) so an
    interrupted write can never leave a truncated/dead token file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)  # atomic on POSIX and Windows


class _CodeCatcher(BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (qs.get("code") or [None])[0]
        err = (qs.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        msg = "Authorization received - you can close this tab." if _CodeCatcher.code \
            else f"Authorization failed: {err}"
        self.wfile.write(msg.encode())

    def log_message(self, *_):  # keep the console quiet
        pass


def _post_token(payload: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def authorize(client_id: str, client_secret: str) -> dict:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",   # always show consent -> fresh refresh token
        "scope": SCOPE,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening browser for consent (or open this URL yourself):\n\n" + url + "\n")
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8765), _CodeCatcher)
    server.timeout = 300
    while _CodeCatcher.code is None:
        server.handle_request()
    server.server_close()

    tokens = _post_token({
        "grant_type": "authorization_code",
        "code": _CodeCatcher.code,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    tokens["obtained_at"] = int(time.time())
    return tokens


def refresh_access_token(client_id: str, client_secret: str, path: Path | None = None) -> str:
    """Rotation-safe refresh: exchange the stored refresh token, then persist the
    NEW access+refresh tokens ATOMICALLY and BEFORE returning the access token to
    any caller that might fail. Returns the fresh access token.

    Contrast sync/authorize.py's Google helper, which returns only the access
    token and never writes back — correct there (stable refresh token), fatal
    here (Strava invalidates the old refresh token the instant it issues a new
    one)."""
    path = path or token_path()
    stored = json.loads(path.read_text(encoding="utf-8"))
    tokens = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": stored["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
    })
    merged = {**stored, **tokens, "obtained_at": int(time.time())}
    _write_token_atomic(path, merged)  # persist the rotated refresh_token first
    return tokens["access_token"]


def api_get(access_token: str, path: str, params: dict | None = None) -> tuple[int, str]:
    url = f"{api_base()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def probe(access_token: str) -> None:
    status, body = api_get(access_token, "/athlete/activities", {"per_page": 1})
    print(f"\n=== GET /athlete/activities (1) -> HTTP {status}")
    print(body[:800])


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe-only", action="store_true",
                    help="skip auth; refresh stored token and probe the API")
    args = ap.parse_args()

    client_id = os.environ.get("STRAVA_CLIENT_ID", "")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ERROR: set STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in .env", file=sys.stderr)
        return 2

    tp = token_path()
    if not args.probe_only:
        tokens = authorize(client_id, client_secret)
        if "refresh_token" not in tokens:
            print("ERROR: no refresh_token in response:", json.dumps(tokens)[:400], file=sys.stderr)
            return 1
        _write_token_atomic(tp, tokens)
        print(f"Token stored at {tp} (scope granted: {SCOPE})")

    access = refresh_access_token(client_id, client_secret, tp)
    print("Refresh-token exchange OK (non-interactive access works; token rotated).")
    probe(access)
    return 0


if __name__ == "__main__":
    sys.exit(main())
