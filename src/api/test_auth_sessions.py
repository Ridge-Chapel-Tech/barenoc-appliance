#!/usr/bin/env python3
"""P0 auth hardening tests (2026-08-25): token revocation + fail-closed JWT.

Covers:
  1. login mints an access + refresh token AND records a revocable session row.
  2. /refresh issues a new access token from a valid refresh (header or cookie).
  3. /logout revokes the session instantly — the refresh token can't be
     replayed (401 on /refresh afterwards).
  4. Password change bumps token_version: old access AND old refresh die
     immediately (401).
  5. Fail-closed JWT: a refresh token never authenticates as an access token;
     tampered / expired / absent tokens all 401.

    cd src/api && python3 -m unittest test_auth_sessions -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="auth-sessions-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt as _jwt  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User, AuthSession  # noqa: E402
from auth import hash_password, decode_token, SECRET_KEY, ALGORITHM  # noqa: E402

init_db()

from main import app  # noqa: E402


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        # The suite runs all test modules in ONE process against a shared
        # engine (run_tests.sh convention) — other modules wipe tables in
        # their own setUp, so alice must be (re)created here, not at module
        # level. Also reset her password/version + drop sessions: tests below
        # change both (the password-change test bumps token_version).
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "alice").first()
            if user is None:
                user = User(username="alice", email="alice@test.local",
                            hashed_password=hash_password("alice-password-123"),
                            role="admin", is_active=True, must_change_password=False)
                db.add(user)
            else:
                user.hashed_password = hash_password("alice-password-123")
                user.token_version = 0
                user.must_change_password = False
                user.role = "admin"
                user.is_active = True
            # compliance controls: reset MFA + lockout state between tests
            user.otp_secret = None
            user.otp_verified = False
            user.failed_logins = 0
            user.locked_until = None
            db.query(AuthSession).delete()
            db.commit()
        finally:
            db.close()
        self.c = TestClient(app)
        r = self.c.post("/api/v1/auth/login",
                        json={"username": "alice", "password": "alice-password-123"})
        self.assertEqual(r.status_code, 200, r.text)
        self.body = r.json()
        self.access = self.body["access_token"]
        self.refresh = self.body["refresh_token"]

    def _decode(self, token):
        return decode_token(token)

    # ── sessions + revocation ──

    def test_login_mints_revocable_session(self):
        payload = self._decode(self.refresh)
        self.assertEqual(payload["type"], "refresh")
        self.assertIn("jti", payload)
        db = SessionLocal()
        try:
            row = db.query(AuthSession).filter(AuthSession.jti == payload["jti"]).first()
            self.assertIsNotNone(row, "session row must be recorded")
            self.assertIsNone(row.revoked_at)
            self.assertEqual(row.user_id, db.query(User).filter(User.username == "alice").first().id)
        finally:
            db.close()
        # refresh cookie is HttpOnly + same-site; access cookie is JS-readable
        # (the SPA reads it into localStorage) — both same-site lax.
        cookies = self.c.post("/api/v1/auth/login",
                              json={"username": "alice", "password": "alice-password-123"})
        set_cookies = cookies.headers.get_list("set-cookie")
        refresh_c = next((c for c in set_cookies if "refresh_token=" in c), "")
        access_c = next((c for c in set_cookies if "access_token=" in c), "")
        self.assertIn("refresh_token=", refresh_c)
        self.assertIn("HttpOnly", refresh_c)
        self.assertIn("SameSite=lax", refresh_c)
        self.assertIn("access_token=", access_c)
        self.assertNotIn("HttpOnly", access_c)
        self.assertIn("SameSite=lax", access_c)

    def test_me_with_access_token(self):
        r = self.c.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {self.access}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "alice")

    def test_refresh_issues_new_access(self):
        r = self.c.post("/api/v1/auth/refresh",
                        headers={"Authorization": f"Bearer {self.refresh}"})
        self.assertEqual(r.status_code, 200, r.text)
        new_access = r.json()["access_token"]
        self.assertNotEqual(new_access, self.access)
        self.assertEqual(self._decode(new_access)["type"], "access")

    def test_refresh_via_cookie(self):
        # TestClient carries the login cookies; no Authorization header → cookie path
        r = self.c.post("/api/v1/auth/refresh")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._decode(r.json()["access_token"])["type"], "access")

    def test_logout_revokes_refresh_instantly(self):
        r = self.c.post("/api/v1/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revoked"], 1)
        # the same refresh token must now be rejected
        r2 = self.c.post("/api/v1/auth/refresh",
                         headers={"Authorization": f"Bearer {self.refresh}"})
        self.assertEqual(r2.status_code, 401)
        self.assertIn("revoked", r2.json().get("detail", ""))

    def test_password_change_revokes_everything(self):
        r = self.c.post("/api/v1/auth/change-password",
                        json={"current_password": "alice-password-123",
                              "new_password": "brand-new-password-456"})
        self.assertEqual(r.status_code, 200, r.text)
        # old access token is dead (ver bump)
        r2 = self.c.get("/api/v1/auth/me",
                        headers={"Authorization": f"Bearer {self.access}"})
        self.assertEqual(r2.status_code, 401)
        # old refresh token is dead too (ver bump + session revoked)
        r3 = self.c.post("/api/v1/auth/refresh",
                         headers={"Authorization": f"Bearer {self.refresh}"})
        self.assertEqual(r3.status_code, 401)

    # ── fail-closed JWT ──

    def test_refresh_token_never_authenticates_as_access(self):
        # A refresh token handed to an API endpoint (Bearer) must 401 — even
        # though it is signed by the same key and carries sub.
        r = self.c.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {self.refresh}"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("token type", r.json()["detail"].lower())

    def test_missing_refresh_is_401(self):
        r = self.c.post("/api/v1/auth/refresh",
                        headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(r.status_code, 401)

    def test_tampered_token_rejected(self):
        bad = self.access[:-4] + "AAAA"  # corrupt the signature
        r = self.c.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {bad}"})
        self.assertEqual(r.status_code, 401)

    def test_expired_token_rejected(self):
        from auth import create_access_token
        expired = create_access_token({"sub": "alice", "role": "admin"},
                                      expires_minutes=-1, ver=0)
        r = self.c.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {expired}"})
        self.assertEqual(r.status_code, 401)

    def test_oidc_flow_token_rejected_as_access(self):
        # A flow/state token must never authenticate API paths either.
        flow = _jwt.encode({"sub": "alice", "type": "oidc_flow",
                            "exp": 9999999999}, SECRET_KEY, algorithm=ALGORITHM)
        r = self.c.get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {flow}"})
        self.assertEqual(r.status_code, 401)

    def test_register_mints_session(self):
        r = self.c.post("/api/v1/auth/register",
                        json={"username": "bobnew", "password": "bob-password-123",
                              "email": "bobnew@test.local"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("refresh_token", r.json())
        payload = self._decode(r.json()["refresh_token"])
        db = SessionLocal()
        try:
            row = db.query(AuthSession).filter(AuthSession.jti == payload["jti"]).first()
            self.assertIsNotNone(row)
        finally:
            db.close()

    # ── compliance: session policy (idle timeout + lockout) ──

    def test_idle_timeout_revokes_refresh(self):
        db = SessionLocal()
        try:
            alice_id = db.query(User).filter(User.username == "alice").first().id
            sess = db.query(AuthSession).filter(
                AuthSession.user_id == alice_id).order_by(
                AuthSession.id.desc()).first()
            sess.last_used_at = datetime.utcnow() - timedelta(minutes=10)
            db.commit()
        finally:
            db.close()
        with patch("routes.auth._session_idle_min", return_value=1):
            r = self.c.post("/api/v1/auth/refresh",
                            headers={"Authorization": f"Bearer {self.refresh}"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("idle", r.json()["detail"].lower())

    def test_lockout_after_failures(self):
        with patch("routes.auth._session_lockout_after", return_value=3):
            for _ in range(3):
                r = self.c.post("/api/v1/auth/login",
                                json={"username": "alice", "password": "wrong"})
                self.assertEqual(r.status_code, 401)
            # even the correct password is now refused while locked
            r = self.c.post("/api/v1/auth/login",
                            json={"username": "alice",
                                  "password": "alice-password-123"})
            self.assertEqual(r.status_code, 423)
            self.assertIn("locked", r.json()["detail"].lower())

    # ── compliance: MFA enforcement (TOTP gate) ──

    def test_mfa_enforced_password_only_401(self):
        with patch("mfa.mfa_enforced", return_value=True):
            r = self.c.post("/api/v1/auth/login",
                            json={"username": "alice",
                                  "password": "alice-password-123"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("MFA required", r.json()["detail"])

    def test_mfa_totp_flow_end_to_end(self):
        import pyotp
        from mfa import generate_secret
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "alice").first()
            user.otp_secret = generate_secret()
            user.otp_verified = True
            db.commit()
            code = pyotp.TOTP(user.otp_secret).now()
        finally:
            db.close()
        with patch("mfa.mfa_enforced", return_value=True):
            r = self.c.post("/api/v1/auth/login",
                            json={"username": "alice",
                                  "password": "alice-password-123",
                                  "totp_code": code})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("access_token", r.json())

    def test_mfa_wrong_totp_401(self):
        import pyotp
        from mfa import generate_secret
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "alice").first()
            user.otp_secret = generate_secret()
            user.otp_verified = True
            db.commit()
            # a code from a DIFFERENT secret must never verify
            wrong = pyotp.TOTP(generate_secret()).now()
        finally:
            db.close()
        with patch("mfa.mfa_enforced", return_value=True):
            r = self.c.post("/api/v1/auth/login",
                            json={"username": "alice",
                                  "password": "alice-password-123",
                                  "totp_code": wrong})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
