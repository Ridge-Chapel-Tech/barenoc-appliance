#!/usr/bin/env python3
"""Offsite/remote backup (Layer 4) tests — encryption round-trip, S3 signing,
upload success/failure, plan-key gating, BYO validation, status record.

    docker compose exec api python3 -m unittest test_remote_backup -v
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="remote-backup-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

import remote_backup as rb


def _fake_client(**overrides):
    """A recording S3Client stand-in for the job (no network)."""
    calls = []

    class Fake:
        def __init__(self, endpoint, region, access_key, secret_key):
            self.endpoint = endpoint
            self.region = region
            self.access_key = access_key
            self.secret_key = secret_key

        def put_object(self, bucket, key, data):
            calls.append(("put", bucket, key, data))
            return overrides.get("put", (200, b"", {}))

        def list_objects(self, bucket, prefix="", max_keys=1000):
            calls.append(("list", bucket, prefix))
            return overrides.get("list", (200, json.dumps({"Contents": []}).encode(), {}))

        def delete_object(self, bucket, key):
            calls.append(("delete", bucket, key))
            return overrides.get("delete", (204, b"", {}))

    return Fake, calls


class EncryptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-enc-")
        self.orig_dek = rb.DEK_FILE
        rb.DEK_FILE = os.path.join(self.tmp, "offsite-dek.key")

    def tearDown(self):
        rb.DEK_FILE = self.orig_dek

    def test_recovery_key_roundtrip(self):
        raw = rb.ensure_dek()
        self.assertEqual(len(raw), 32)
        encoded = rb.encode_recovery_key(raw)
        # uppercase base32, no padding, grouped in 4s
        self.assertTrue(encoded.isupper())
        self.assertEqual(encoded, "-".join(encoded.split("-")))
        self.assertEqual(rb.decode_recovery_key(encoded), raw)
        # tolerant of whitespace/lowercase/dashes
        self.assertEqual(rb.decode_recovery_key(encoded.lower().replace("-", " ")), raw)

    def test_encrypt_decrypt_roundtrip(self):
        raw = rb.ensure_dek()
        plain = os.path.join(self.tmp, "in.tar.gz")
        enc = os.path.join(self.tmp, "out.enc")
        dec = os.path.join(self.tmp, "out.tar.gz")
        payload = b"\x00\x01\x02hello offsite\xff" * 1000
        with open(plain, "wb") as f:
            f.write(payload)
        size = rb.encrypt_file_to(plain, enc)
        self.assertGreater(size, len(payload))
        self.assertEqual(os.stat(enc).st_mode & 0o777, 0o600)
        n = rb.decrypt_file_to(enc, dec, raw)
        self.assertEqual(n, len(payload))
        with open(dec, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_decrypt_with_recovery_key_string(self):
        raw = rb.ensure_dek()
        plain = os.path.join(self.tmp, "p.tar.gz")
        enc = os.path.join(self.tmp, "e.enc")
        with open(plain, "wb") as f:
            f.write(b"data")
        rb.encrypt_file_to(plain, enc)
        out = os.path.join(self.tmp, "d.tar.gz")
        rb.decrypt_archive_with_recovery_key(enc, out, rb.encode_recovery_key(raw))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"data")

    def test_decrypt_wrong_key_fails(self):
        raw = rb.ensure_dek()
        plain = os.path.join(self.tmp, "p2.tar.gz")
        enc = os.path.join(self.tmp, "e2.enc")
        with open(plain, "wb") as f:
            f.write(b"secret")
        rb.encrypt_file_to(plain, enc)
        with self.assertRaises(Exception):
            rb.decrypt_file_to(enc, os.path.join(self.tmp, "x.tar.gz"), os.urandom(32))

    def test_decrypt_bad_magic_fails(self):
        bad = os.path.join(self.tmp, "bad.enc")
        with open(bad, "wb") as f:
            f.write(b"not-an-offsite-archive\nAAAA\nAAAA\n")
        with self.assertRaises(ValueError):
            rb.decrypt_file_to(bad, os.path.join(self.tmp, "x.tar.gz"), os.urandom(32))

    def test_ensure_dek_returns_whitespace_terminated_key_unchanged(self):
        # Regression (CI flake, "errors=1"): ensure_dek used .strip() on the
        # raw key, so a DEK whose first/last byte happened to be whitespace
        # (0x09-0x0D, 0x20) was silently regenerated on the next read — the
        # encrypt pass then used a DIFFERENT key than the caller held, and the
        # decrypt raised InvalidTag. Read the exact bytes back; never strip.
        raw = os.urandom(32)
        raw = b" " + raw[1:-1] + b"\n"   # leading space, trailing newline
        with open(rb.DEK_FILE, "wb") as f:
            f.write(raw)
        self.assertEqual(rb.ensure_dek(), raw)


class PlanKeyTest(unittest.TestCase):
    def test_valid_beta_key(self):
        r = rb.verify_plan_key(rb.BETA_PLAN_KEY)
        self.assertTrue(r["valid"])
        self.assertEqual(r["tier"], "managed")

    def test_invalid_key(self):
        r = rb.verify_plan_key("nope")
        self.assertFalse(r["valid"])
        self.assertIsNone(r["tier"])

    def test_empty_key(self):
        self.assertFalse(rb.verify_plan_key("")["valid"])
        self.assertFalse(rb.verify_plan_key(None)["valid"])

    def test_env_override(self):
        with patch.dict(os.environ, {"BARENOC_BETA_PLAN_KEY": "custom-beta-key"}):
            self.assertTrue(rb.verify_plan_key("custom-beta-key")["valid"])
            self.assertFalse(rb.verify_plan_key(rb.BETA_PLAN_KEY)["valid"])


class SignerTest(unittest.TestCase):
    def test_uri_encode_preserves_slashes(self):
        self.assertEqual(rb._uri_encode_path("/bucket/a key/b"), "/bucket/a%20key/b")

    def test_canonical_query_sorted(self):
        q = rb._canonical_query({"prefix": "barenoc-1", "list-type": "2"})
        self.assertEqual(q, "list-type=2&prefix=barenoc-1")

    def test_canonical_headers_normalized(self):
        canon, signed = rb._canonical_headers({"Host": "  minio:9000 ", "X-Amz-Date": "x"})
        self.assertIn("host:minio:9000\n", canon)
        self.assertEqual(signed, "host;x-amz-date")

    def test_sign_request_shape(self):
        h = rb.sign_request("PUT", "https://minio:9000/bucket/key.enc",
                            "us-east-1", "AK", "SK", payload=b"abc")
        self.assertIn("Authorization", h)
        self.assertIn("AWS4-HMAC-SHA256 Credential=AK/", h["Authorization"])
        self.assertIn("/us-east-1/s3/aws4_request", h["Authorization"])
        self.assertIn("SignedHeaders=host;x-amz-content-sha256;x-amz-date", h["Authorization"])
        self.assertIn("Signature=", h["Authorization"])
        self.assertEqual(h["x-amz-content-sha256"], rb._sha256_hex(b"abc"))

    def test_sign_request_deterministic_same_payload(self):
        fixed = datetime.datetime(2026, 8, 30, 3, 15, 0,
                                  tzinfo=datetime.timezone.utc)
        a = rb.sign_request("GET", "https://m:9000/b", "r", "AK", "SK",
                            now=fixed)
        b = rb.sign_request("GET", "https://m:9000/b", "r", "AK", "SK",
                            now=fixed)
        self.assertEqual(a["Authorization"], b["Authorization"])
        # a different clock produces a different signature (the date/amz-date
        # are part of the canonical request), proving `now` is actually used.
        c = rb.sign_request("GET", "https://m:9000/b", "r", "AK", "SK",
                            now=fixed + datetime.timedelta(seconds=1))
        self.assertNotEqual(a["Authorization"], c["Authorization"])


class S3ClientTest(unittest.TestCase):
    def test_endpoint_normalization(self):
        c = rb.S3Client("minio.example.com:9000", "us-east-1", "AK", "SK")
        self.assertEqual(c.scheme, "https")
        self.assertEqual(c.host, "minio.example.com:9000")
        self.assertEqual(c._url("b", "k"), "https://minio.example.com:9000/b/k")

    def test_endpoint_rejects_path(self):
        with self.assertRaises(ValueError):
            rb.S3Client("https://host:9000/base/", "r", "AK", "SK")


class ResolveTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-cfg-")
        self.orig = (rb.OFFSITE_CONF, rb.OFFSITE_CREDS)
        rb.OFFSITE_CONF = os.path.join(self.tmp, "offsite.conf")
        rb.OFFSITE_CREDS = os.path.join(self.tmp, "offsite-creds")

    def tearDown(self):
        rb.OFFSITE_CONF, rb.OFFSITE_CREDS = self.orig

    def test_off_mode_raises(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "off"})
        with self.assertRaises(rb.OffsiteError):
            rb.resolve_target(rb.read_offsite_conf())

    def test_managed_without_plan_key_blocked(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "managed", "PLAN_KEY": ""})
        with self.assertRaises(rb.OffsiteError) as cm:
            rb.resolve_target(rb.read_offsite_conf())
        self.assertIn("plan key", str(cm.exception))

    def test_managed_with_valid_key_but_no_backend(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "managed", "PLAN_KEY": rb.BETA_PLAN_KEY})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OFFSITE_MANAGED_ENDPOINT", None)
            os.environ.pop("OFFSITE_MANAGED_BUCKET", None)
            with self.assertRaises(rb.OffsiteError) as cm:
                rb.resolve_target(rb.read_offsite_conf())
            self.assertIn("not provisioned", str(cm.exception))

    def test_managed_resolves_with_env_backend(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "managed", "PLAN_KEY": rb.BETA_PLAN_KEY})
        env = {"OFFSITE_MANAGED_ENDPOINT": "https://omv:9000",
               "OFFSITE_MANAGED_BUCKET": "barenoc-managed",
               "OFFSITE_MANAGED_ACCESS_KEY": "AK",
               "OFFSITE_MANAGED_SECRET_KEY": "SK",
               "OFFSITE_MANAGED_PREFIX": "barenoc-7"}
        t = rb.resolve_target(rb.read_offsite_conf(), env)
        self.assertEqual(t["bucket"], "barenoc-managed")
        self.assertEqual(t["prefix"], "barenoc-7")

    def test_byo_incomplete_blocked(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "byo", "BYO_ENDPOINT": "https://x:9000",
                               "BYO_BUCKET": "b"})
        with patch.object(rb, "read_offsite_credentials",
                          return_value={"access_key": "", "secret": ""}):
            with self.assertRaises(rb.OffsiteError):
                rb.resolve_target(rb.read_offsite_conf())

    def test_byo_resolves(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "byo", "BYO_ENDPOINT": "https://x:9000",
                               "BYO_BUCKET": "b", "BYO_PREFIX": "p", "BYO_REGION": "r"})
        with patch.object(rb, "read_offsite_credentials",
                          return_value={"access_key": "AK", "secret": "SK"}):
            t = rb.resolve_target(rb.read_offsite_conf())
        self.assertEqual(t["mode"], "byo")
        self.assertEqual(t["prefix"], "p")
        self.assertEqual(t["region"], "r")


class ScheduleTest(unittest.TestCase):
    def test_should_run_now_daily_at_hour(self):
        cfg = {"OFF_SITE_HOUR": "3", "OFF_SITE_DAY": "daily"}
        now = datetime.datetime(2026, 8, 30, 3, 15)
        self.assertTrue(rb.should_run_now(cfg, now))
        self.assertFalse(rb.should_run_now(cfg, now.replace(hour=4)))

    def test_should_run_now_weekly(self):
        cfg = {"OFF_SITE_HOUR": "3", "OFF_SITE_DAY": "0"}  # Sunday
        sunday = datetime.datetime(2026, 8, 30, 3, 15)     # 2026-08-30 is a Sunday
        self.assertEqual((sunday.weekday() + 1) % 7, 0)
        self.assertTrue(rb.should_run_now(cfg, sunday))
        self.assertFalse(rb.should_run_now(cfg, sunday.replace(day=31)))  # Monday

    def test_next_run_future(self):
        cfg = {"OFF_SITE_HOUR": "3", "OFF_SITE_DAY": "daily"}
        now = datetime.datetime(2026, 8, 30, 4, 0)
        nxt = datetime.datetime.fromisoformat(rb.next_run(cfg, now))
        self.assertGreater(nxt, now)
        self.assertEqual(nxt.hour, 3)


class RunOffsiteBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-run-")
        self.orig = (rb.BACKUP_DIR, rb.DEK_FILE, rb.OFFSITE_CONF, rb.OFFSITE_CREDS,
                     rb.OFFSITE_STATUS_JSON, rb.OFFSITE_STORE, rb.LATEST_ARCHIVE_POINTER)
        rb.BACKUP_DIR = os.path.join(self.tmp, "backups")
        rb.DEK_FILE = os.path.join(self.tmp, "db", "offsite-dek.key")
        rb.OFFSITE_CONF = os.path.join(self.tmp, "status", "offsite.conf")
        rb.OFFSITE_CREDS = os.path.join(self.tmp, "status", "offsite-creds")
        rb.OFFSITE_STATUS_JSON = os.path.join(self.tmp, "status", "offsite_status.json")
        rb.OFFSITE_STORE = os.path.join(self.tmp, "backups", "offsite")
        rb.LATEST_ARCHIVE_POINTER = os.path.join(self.tmp, "backups", "latest_archive")
        os.makedirs(rb.BACKUP_DIR, exist_ok=True)
        # a fake app-data archive
        self.archive = os.path.join(rb.BACKUP_DIR, "app-backup-20260830-030000.tar.gz")
        with open(self.archive, "wb") as f:
            f.write(b"fake app archive payload")
        with open(rb.LATEST_ARCHIVE_POINTER, "w") as f:
            f.write(self.archive)

    def tearDown(self):
        (rb.BACKUP_DIR, rb.DEK_FILE, rb.OFFSITE_CONF, rb.OFFSITE_CREDS,
         rb.OFFSITE_STATUS_JSON, rb.OFFSITE_STORE, rb.LATEST_ARCHIVE_POINTER) = self.orig

    def _write_byo_conf(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "byo", "BYO_ENDPOINT": "https://x:9000",
                               "BYO_BUCKET": "b", "BYO_REGION": "us-east-1"})

    def test_off_mode_noop(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "off"})
        self.assertEqual(rb.run_offsite_backup(force=True), 0)
        status = rb.read_offsite_status()
        self.assertFalse(status["enabled"])

    def test_managed_without_plan_key_fails_status(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "managed", "PLAN_KEY": ""})
        self.assertEqual(rb.run_offsite_backup(force=True), 1)
        status = rb.read_offsite_status()
        self.assertTrue(status["last_failed"])
        self.assertIn("plan key", status["last_error"])

    def test_upload_success_writes_status_and_encrypts(self):
        self._write_byo_conf()
        Fake, calls = _fake_client()
        with patch.object(rb, "S3Client", Fake), \
             patch.object(rb, "read_offsite_credentials",
                          return_value={"access_key": "AK", "secret": "SK"}):
            rc = rb.run_offsite_backup(force=True)
        self.assertEqual(rc, 0)
        status = rb.read_offsite_status()
        self.assertTrue(status["last_ok"])
        self.assertFalse(status["last_failed"])
        self.assertGreater(status["last_size_bytes"], 0)
        self.assertIn("offsite-", status["object_key"])
        # exactly one PUT, to the right bucket/key
        puts = [c for c in calls if c[0] == "put"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], "b")
        self.assertTrue(puts[0][2].endswith(".enc"))
        # the encrypted local copy decrypts back to the source
        enc_files = rb.list_local_encrypted()
        self.assertEqual(len(enc_files), 1)
        raw = rb.ensure_dek()
        out = os.path.join(self.tmp, "roundtrip.tar.gz")
        rb.decrypt_file_to(enc_files[0], out, raw)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"fake app archive payload")

    def test_upload_failure_writes_status(self):
        self._write_byo_conf()
        Fake, _ = _fake_client(put=(403, b"denied", {}))
        with patch.object(rb, "S3Client", Fake), \
             patch.object(rb, "read_offsite_credentials",
                          return_value={"access_key": "AK", "secret": "SK"}):
            rc = rb.run_offsite_backup(force=True)
        self.assertEqual(rc, 1)
        status = rb.read_offsite_status()
        self.assertTrue(status["last_failed"])
        self.assertIn("403", status["last_error"])
        # failed upload removes the local encrypted artifact
        self.assertEqual(rb.list_local_encrypted(), [])

    def test_no_archive_yet_fails_cleanly(self):
        os.remove(self.archive)
        os.remove(rb.LATEST_ARCHIVE_POINTER)
        self._write_byo_conf()
        with patch.object(rb, "read_offsite_credentials",
                          return_value={"access_key": "AK", "secret": "SK"}):
            rc = rb.run_offsite_backup(force=True)
        self.assertEqual(rc, 1)
        self.assertIn("no app-data archive", rb.read_offsite_status()["last_error"])

    def test_schedule_gate_skips_off_hour(self):
        self._write_byo_conf()
        now = datetime.datetime(2026, 8, 30, 12, 15)  # not hour 3
        self.assertEqual(rb.run_offsite_backup(force=False, now=now), 0)
        self.assertEqual(rb.list_local_encrypted(), [])


class ConfigRoundtripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-conf-")
        self.orig = rb.OFFSITE_CONF
        rb.OFFSITE_CONF = os.path.join(self.tmp, "offsite.conf")

    def tearDown(self):
        rb.OFFSITE_CONF = self.orig

    def test_write_read_roundtrip(self):
        rb.write_offsite_conf({"OFF_SITE_MODE": "byo", "BYO_BUCKET": "b",
                               "PLAN_KEY": "k", "OFF_SITE_HOUR": "5"})
        cfg = rb.read_offsite_conf()
        self.assertEqual(cfg["OFF_SITE_MODE"], "byo")
        self.assertEqual(cfg["BYO_BUCKET"], "b")
        self.assertEqual(cfg["PLAN_KEY"], "k")
        self.assertEqual(cfg["OFF_SITE_HOUR"], "5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
