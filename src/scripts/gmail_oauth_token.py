#!/usr/bin/env python3
"""Gmail OAuth refresh-token generator for BareNOC.

Runs the one-time Google consent flow on your machine and prints the
refresh token to paste into BareNOC (Settings -> Email -> Gmail OAuth2).

Setup (Google Cloud Console):
  1. Enable the Gmail API.
  2. OAuth consent screen: scope https://www.googleapis.com/auth/gmail.send,
     user type External, test user = the Gmail account that will send.
  3. Credentials -> Create OAuth client ID -> **Desktop app** (loopback
     redirect http://localhost needs no registration).

Usage:
  python3 gmail_oauth_token.py --client-id <CLIENT_ID> --client-secret <SECRET>
      [--port 8765]

Prints (and copies to clipboard when possible) the refresh token.
"""

import argparse
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


def main():
    ap = argparse.ArgumentParser(description="Generate a Gmail OAuth refresh token for BareNOC")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--wait-minutes", type=float, default=10.0,
                    help="how long to wait for the browser callback")
    args = ap.parse_args()

    redirect_uri = f"http://127.0.0.1:{args.port}"
    state = secrets.token_urlsafe(16)
    code_holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch")
                return
            if "code" in q:
                code_holder["code"] = q["code"][0]
                body = b"<h3>Got the code - close this tab.</h3>"
                self.send_response(200)
            else:
                err = q.get("error", ["unknown"])[0]
                body = f"<h3>Error: {err}</h3>".encode()
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    auth_url = ("https://accounts.google.com/o/oauth2/v2/auth?"
                f"client_id={urllib.parse.quote(args.client_id)}"
                f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
                f"&response_type=code&scope={urllib.parse.quote(SCOPE)}"
                f"&access_type=offline&prompt=consent&state={state}")
    print(f"1. Open this URL in the browser (as the Gmail account that will send):\n\n   {auth_url}\n")
    print("   (Or paste it into a browser on any machine — the callback returns here.)\n")

    deadline = int(args.wait_minutes * 60)
    print(f"2. Waiting up to {deadline}s for the redirect to {redirect_uri} ...")
    for _ in range(deadline * 10):
        if "code" in code_holder:
            break
        import time
        time.sleep(0.1)
    srv.shutdown()

    if "code" not in code_holder:
        print("No code received in time. Re-run and complete the consent screen.")
        sys.exit(1)

    print("3. Exchanging the code for tokens ...")
    data = urllib.parse.urlencode({
        "code": code_holder["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception as e:
        print(f"Token exchange failed: {e}")
        sys.exit(1)

    if "refresh_token" not in resp:
        print("No refresh_token in response — did you use prompt=consent?")
        print(json.dumps(resp, indent=1))
        sys.exit(1)

    print("\n=== REFRESH TOKEN (paste into BareNOC Settings -> Email) ===\n")
    print(resp["refresh_token"])
    print("\n=== also record these (needed at runtime) ===")
    print(f"Client ID:     {args.client_id}")
    print(f"Client Secret: {args.client_secret}")
    print("Sender (GOOGLE_SENDER): the Gmail account address")

    try:
        import subprocess
        subprocess.run(["xclip", "-selection", "clipboard"], input=resp["refresh_token"].encode(),
                       check=True)
        print("\n(copied to clipboard)")
    except Exception:
        pass


if __name__ == "__main__":
    main()
