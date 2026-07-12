"""One-time interactive Google OAuth authorization for the Health API.

    python -m sync.authorize [--probe-only]

Opens a browser for consent, catches the redirect on http://localhost:8765/,
exchanges the code, and stores the token file at GOOGLE_TOKEN_PATH (default
./secrets/google_token.json — gitignored). With --probe-only, skips auth and
just exercises the API with the stored token (refresh + a few reads).

Stdlib only; .env in the current directory is read automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4"
REDIRECT_URI = "http://localhost:8765/"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def token_path() -> Path:
    return Path(os.environ.get("GOOGLE_TOKEN_PATH", "./secrets/google_token.json"))


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
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # force a fresh refresh token even on re-auth
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
        "redirect_uri": REDIRECT_URI,
    })
    tokens["obtained_at"] = int(time.time())
    return tokens


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    tokens = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    return tokens["access_token"]


def api_get(access_token: str, path: str, params: dict | None = None) -> tuple[int, str]:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def api_post(access_token: str, path: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def probe(access_token: str) -> None:
    """Gentle reads exercising the shapes from the v4 discovery document."""
    from datetime import date, timedelta
    y = date.today() - timedelta(days=1)
    civil = lambda d: {"year": d.year, "month": d.month, "day": d.day}

    # intraday list: AIP-160 filter; heart rate is a *sample* data type
    flt = (f'heart_rate.sample_time.physical_time >= "{y.isoformat()}T08:00:00Z" '
           f'AND heart_rate.sample_time.physical_time < "{y.isoformat()}T08:10:00Z"')
    status, body = api_get(access_token, "/users/me/dataTypes/heart-rate/dataPoints",
                           {"filter": flt, "pageSize": 5})
    print(f"\n=== LIST heart-rate (10 min window) -> HTTP {status}")
    print(body[:1200])

    # daily rollup: POST with a civil-time range
    status, body = api_post(
        access_token, "/users/me/dataTypes/steps/dataPoints:dailyRollUp",
        {"range": {"start": {"date": civil(y)}, "end": {"date": civil(date.today())}}})
    print(f"\n=== dailyRollUp steps ({y}) -> HTTP {status}")
    print(body[:1200])


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe-only", action="store_true",
                    help="skip auth; refresh stored token and probe the API")
    args = ap.parse_args()

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("ERROR: set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env", file=sys.stderr)
        return 2

    tp = token_path()
    if not args.probe_only:
        tokens = authorize(client_id, client_secret)
        if "refresh_token" not in tokens:
            print("ERROR: no refresh_token in response:", json.dumps(tokens)[:400], file=sys.stderr)
            return 1
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        try:
            os.chmod(tp, 0o600)
        except OSError:
            pass
        print(f"Token stored at {tp} (scopes granted: {tokens.get('scope', '?')})")

    stored = json.loads(tp.read_text(encoding="utf-8"))
    access = refresh_access_token(client_id, client_secret, stored["refresh_token"])
    print("Refresh-token exchange OK (non-interactive access works).")
    probe(access)
    return 0


if __name__ == "__main__":
    sys.exit(main())
