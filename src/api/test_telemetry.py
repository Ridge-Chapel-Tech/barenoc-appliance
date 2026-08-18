#!/usr/bin/env python3
"""In-container tests for the telemetry backbone (P0 time-series).

Covers: the metrics store (batched write/read, trends bucketing + gaps,
retention pruning), the counter->rate math, the collectors (fake controller /
fake SNMP / fake ping — no live gear), and admin/operator gating. Uses a
scratch sqlite DB.

    docker compose exec api python3 -m unittest test_telemetry -v
"""

import datetime
import math
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="telemetry-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from database import SessionLocal, init_db
from models import User, Device, Metric, Ticket, ScanRun, Finding
import metrics_store
import telemetry
from routes import metrics as routes


def _dt(*args):
    return datetime.datetime(*args)


# ═══════════════════════════════ store ═════════════════════════════════════

class StoreTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for t in (Metric, Finding, ScanRun, Ticket, Device, User):
            db.query(t).delete()
        db.commit()
        db.close()
        db = SessionLocal()
        d = Device(name="core-sw", ip_address="10.0.0.2", device_type="switch",
                   claimed=True)
        db.add(d)
        db.commit()
        db.refresh(d)
        self.device_id = d.id
        db.close()

    def _add(self, metric, value, ts):
        db = SessionLocal()
        db.add(Metric(device_id=self.device_id, metric=metric, ts=ts, value=value))
        db.commit()
        db.close()

    def test_write_samples_batches_and_skips_invalid(self):
        db = SessionLocal()
        base = _dt(2026, 8, 18, 12, 0, 0)
        samples = [
            {"device_id": self.device_id, "metric": "ping.latency_ms",
             "ts": base, "value": 1.0},
            {"device_id": self.device_id, "metric": "ping.latency_ms",
             "ts": base + datetime.timedelta(seconds=1), "value": 2.0},
            {"device_id": self.device_id, "metric": "ping.latency_ms",
             "ts": base + datetime.timedelta(seconds=2), "value": 3.0},
            # invalid — must be skipped, not raise
            {"device_id": None, "metric": "x", "ts": base, "value": 1.0},
            {"device_id": self.device_id, "metric": "  ", "ts": base, "value": 1.0},
            {"device_id": self.device_id, "metric": "nan", "ts": base, "value": float("nan")},
            {"device_id": self.device_id, "metric": "inf", "ts": base, "value": float("inf")},
        ]
        written = metrics_store.write_samples(db, samples)
        db.close()
        self.assertEqual(written, 3)
        db = SessionLocal()
        self.assertEqual(db.query(Metric).count(), 3)
        db.close()

    def test_write_samples_requires_existing_device(self):
        # FK is enforced (foreign_keys=ON) — a bad device_id must raise.
        db = SessionLocal()
        with self.assertRaises(Exception):
            metrics_store.write_samples(db, [
                {"device_id": 999999, "metric": "x", "ts": _dt(2026, 8, 18),
                 "value": 1.0},
            ])
        db.rollback()
        db.close()

    def test_trends_bucketing_min_avg_max(self):
        base = _dt(2026, 8, 18, 12, 0, 0)
        for i in range(10):
            self._add("ping.latency_ms", float(i + 1), base + datetime.timedelta(seconds=i))
        db = SessionLocal()
        pts = metrics_store.trends(db, self.device_id, "ping.latency_ms", base,
                                   base + datetime.timedelta(seconds=10), "avg",
                                   max_buckets=2)
        db.close()
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0]["n"], 5)
        self.assertEqual(pts[0]["value"], 3.0)   # avg of 1..5
        self.assertEqual(pts[1]["value"], 8.0)   # avg of 6..10
        db = SessionLocal()
        mn = metrics_store.trends(db, self.device_id, "ping.latency_ms", base,
                                  base + datetime.timedelta(seconds=10), "min",
                                  max_buckets=2)
        mx = metrics_store.trends(db, self.device_id, "ping.latency_ms", base,
                                  base + datetime.timedelta(seconds=10), "max",
                                  max_buckets=2)
        db.close()
        self.assertEqual(mn[0]["value"], 1.0)
        self.assertEqual(mx[1]["value"], 10.0)

    def test_trends_gaps_are_omitted_not_fabricated(self):
        base = _dt(2026, 8, 18, 12, 0, 0)
        # two clusters 10 minutes apart, nothing in between
        for i in range(2):
            self._add("ping.latency_ms", 5.0, base + datetime.timedelta(seconds=i))
            self._add("ping.latency_ms", 50.0,
                      base + datetime.timedelta(minutes=10, seconds=i))
        db = SessionLocal()
        pts = metrics_store.trends(db, self.device_id, "ping.latency_ms", base,
                                   base + datetime.timedelta(minutes=11), "avg",
                                   max_buckets=20)
        db.close()
        # no zero-filled buckets in the middle: only the two populated buckets
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0]["value"], 5.0)
        self.assertEqual(pts[1]["value"], 50.0)

    def test_retention_math_disk_aware(self):
        self.assertEqual(metrics_store.retention_days(30, 10, free_pct=50.0), 30)
        self.assertEqual(metrics_store.retention_days(30, 10, free_pct=5.0), 7)
        self.assertEqual(metrics_store.retention_days(3, 10, free_pct=5.0), 3)

    def test_prune_deletes_old_rows(self):
        old = datetime.datetime.utcnow() - datetime.timedelta(days=40)
        fresh = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        self._add("ping.latency_ms", 1.0, old)
        self._add("ping.latency_ms", 2.0, fresh)
        db = SessionLocal()
        result = metrics_store.prune(db, days=30, min_free_pct=10)
        db.close()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["retention_days"], 30)
        db = SessionLocal()
        self.assertEqual(db.query(Metric).count(), 1)
        db.close()


# ═══════════════════════════════ counter->rate ═════════════════════════════

class CounterRateTest(unittest.TestCase):
    def test_basic_rate(self):
        t0 = _dt(2026, 8, 18, 12, 0, 0)
        prev = {(1, "m"): (t0, 100)}
        curr = {(1, "m"): (t0 + datetime.timedelta(seconds=10), 200)}
        self.assertAlmostEqual(telemetry.compute_counter_rates(prev, curr)[(1, "m")], 10.0)

    def test_first_seen_skipped(self):
        t0 = _dt(2026, 8, 18, 12, 0, 0)
        curr = {(1, "m"): (t0, 100)}
        self.assertEqual(telemetry.compute_counter_rates({}, curr), {})

    def test_counter_reset_skipped(self):
        t0 = _dt(2026, 8, 18, 12, 0, 0)
        prev = {(1, "m"): (t0, 100)}
        curr = {(1, "m"): (t0 + datetime.timedelta(seconds=5), 50)}  # rolled over
        self.assertEqual(telemetry.compute_counter_rates(prev, curr), {})

    def test_zero_delta_skipped(self):
        t0 = _dt(2026, 8, 18, 12, 0, 0)
        prev = {(1, "m"): (t0, 100)}
        curr = {(1, "m"): (t0, 100)}
        self.assertEqual(telemetry.compute_counter_rates(prev, curr), {})


# ═══════════════════════════════ collectors ════════════════════════════════

class UniFiCollectorTest(unittest.TestCase):
    def test_collect_unifi_metrics(self):
        class FakeClient:
            def get_raw_devices(self):
                return [{
                    "mac": "aa:bb:cc:00:00:01", "state": 1, "uptime": 3600,
                    "latency": 3.2,
                    "stat": {"rx_bytes": 1000, "tx_bytes": 2000},
                    "port_table": [
                        {"port_idx": 3, "up": True, "rx_bytes": 500, "tx_bytes": 600},
                    ],
                }]
        samples = telemetry.collect_unifi_metrics(FakeClient(),
                                                  {"aa:bb:cc:00:00:01": 7})
        by = {(s["metric"], s["kind"]): s for s in samples}
        self.assertEqual(by[("unifi.state", "gauge")]["value"], 1)
        self.assertEqual(by[("unifi.uptime_seconds", "gauge")]["value"], 3600)
        self.assertEqual(by[("unifi.latency_ms", "gauge")]["value"], 3.2)
        self.assertEqual(by[("unifi.port.3.up", "gauge")]["value"], 1)
        self.assertEqual(by[("unifi.port.3.rx_bps", "counter")]["value"], 500)
        self.assertEqual(by[("unifi.rx_bps", "counter")]["value"], 1000)

    def test_collect_unifi_unknown_mac_skipped(self):
        class FakeClient:
            def get_raw_devices(self):
                return [{"mac": "aa:bb:cc:00:00:02", "state": 1}]
        self.assertEqual(telemetry.collect_unifi_metrics(FakeClient(), {}), [])


class PingCollectorTest(unittest.TestCase):
    def test_ping_sample_parses_latency_and_loss(self):
        fake = {"ok": True,
                "stdout": "3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n"
                          "rtt min/avg/max/mdev = 0.5/1.25/2.0/0.3 ms",
                "stderr": ""}
        p = telemetry.ping_sample("10.0.0.2", run=lambda *a, **k: fake)
        self.assertTrue(p["reachable"])
        self.assertEqual(p["latency_ms"], 1.25)
        self.assertEqual(p["loss_pct"], 0.0)

    def test_ping_sample_total_loss(self):
        fake = {"ok": False,
                "stdout": "3 packets transmitted, 0 received, 100% packet loss, time 2000ms",
                "stderr": ""}
        p = telemetry.ping_sample("10.0.0.2", run=lambda *a, **k: fake)
        self.assertFalse(p["reachable"])
        self.assertEqual(p["loss_pct"], 100.0)

    def test_ping_metrics_gauge_samples(self):
        with patch.object(telemetry, "ping_sample",
                          return_value={"reachable": True, "latency_ms": 4.2,
                                        "loss_pct": 0.0}):
            samples = telemetry.collect_ping_metrics(7, "10.0.0.2")
        metrics = {s["metric"]: s["value"] for s in samples}
        self.assertEqual(metrics["ping.reachable"], 1)
        self.assertEqual(metrics["ping.latency_ms"], 4.2)
        self.assertEqual(metrics["ping.loss_pct"], 0.0)


class SnmpCollectorTest(unittest.TestCase):
    def _fake_get(self, ip, community, version, oid):
        vals = {
            "1.3.6.1.2.1.1.1.0": "Cisco IOS",
            "1.3.6.1.2.1.1.3.0": 360000,   # Timeticks -> 3600 s
            "1.3.6.1.4.1.2021.10.1.3.1": 0.42,
            "1.3.6.1.4.1.2021.4.5.0": 8000,
            "1.3.6.1.4.1.2021.4.6.0": 2000,
        }
        return vals.get(oid)

    def test_snmp_sample_and_metrics(self):
        iftable = {
            "1": {"ifdescr": "Gi0/1", "ifoper": 1, "ifinoctets": 1000, "ifoutoctets": 2000},
            "2": {"ifdescr": "Gi0/2", "ifoper": 2, "ifinoctets": 0, "ifoutoctets": 0},
        }
        with patch.object(telemetry, "_snmp_get", side_effect=self._fake_get), \
             patch.object(telemetry, "_snmp_walk_iftable", return_value=iftable):
            snap = telemetry.snmp_sample("10.0.0.2", "public")
            samples = telemetry.collect_snmp_metrics(7, "10.0.0.2", "public")
        self.assertEqual(snap["uptime_seconds"], 3600)
        self.assertEqual(snap["cpu_pct"], 0.42)
        self.assertEqual(snap["mem_used_pct"], 75.0)
        by = {(s["metric"], s["kind"]): s for s in samples}
        self.assertEqual(by[("snmp.cpu_pct", "gauge")]["value"], 0.42)
        self.assertEqual(by[("snmp.mem_used_pct", "gauge")]["value"], 75.0)
        self.assertEqual(by[("snmp.if.Gi0_1.oper_status", "gauge")]["value"], 1)
        self.assertEqual(by[("snmp.if.Gi0_1.rx_bps", "counter")]["value"], 1000)
        self.assertEqual(by[("snmp.if.Gi0_2.oper_status", "gauge")]["value"], 0)

    def test_snmp_no_response_returns_none(self):
        with patch.object(telemetry, "_snmp_get", return_value=None):
            self.assertIsNone(telemetry.snmp_sample("10.0.0.2", "public"))


# ═══════════════════════════════ gating ════════════════════════════════════

class GatingTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        for t in (Metric, Finding, ScanRun, Ticket, Device, User):
            db.query(t).delete()
        db.commit()
        from auth import hash_password
        for name, role in (("admin", "admin"), ("op", "operator"), ("ro", "readonly")):
            db.add(User(username=name, role=role,
                        hashed_password=hash_password("pw"), is_active=True))
        d = Device(name="sw", ip_address="", device_type="switch", claimed=True)
        db.add(d)
        db.commit()
        db.refresh(d)
        self.device_id = d.id
        db.close()
        self.admin = SimpleNamespace(username="admin", role="admin")
        self.op = SimpleNamespace(username="op", role="operator")
        self.ro = SimpleNamespace(username="ro", role="readonly")

    def _client(self, user):
        from main import app
        from fastapi.testclient import TestClient
        from auth import create_access_token
        token = create_access_token({"sub": user.username, "role": user.role,
                                     "groups": [], "auth_method": "password"})
        return TestClient(app), token

    def _auth(self, client, token):
        return {"Authorization": f"Bearer {token}"}

    def test_trends_requires_operator(self):
        client, token = self._client(self.op)
        url = f"/api/v1/metrics/trends?device={self.device_id}&metric=ping.latency_ms"
        self.assertEqual(client.get(url, headers=self._auth(client, token)).status_code, 200)
        client2, token2 = self._client(self.ro)
        self.assertEqual(client2.get(url, headers=self._auth(client2, token2)).status_code, 403)

    def test_catalog_requires_operator(self):
        client, token = self._client(self.op)
        self.assertEqual(client.get("/api/v1/metrics/catalog",
                                    headers=self._auth(client, token)).status_code, 200)
        client2, token2 = self._client(self.ro)
        self.assertEqual(client2.get("/api/v1/metrics/catalog",
                                     headers=self._auth(client2, token2)).status_code, 403)

    def test_unauthenticated_401(self):
        from main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        url = f"/api/v1/metrics/trends?device={self.device_id}&metric=ping.latency_ms"
        self.assertEqual(client.get(url).status_code, 401)
        self.assertEqual(client.post("/api/v1/metrics/ingest", json={"samples": []}).status_code, 401)

    def test_ingest_admin_only(self):
        client, token = self._client(self.admin)
        body = {"samples": [{"device_id": self.device_id, "metric": "ping.latency_ms",
                             "ts": datetime.datetime.utcnow().isoformat(), "value": 1.0}]}
        r = client.post("/api/v1/metrics/ingest", json=body,
                        headers=self._auth(client, token))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["written"], 1)
        client2, token2 = self._client(self.op)
        self.assertEqual(client2.post("/api/v1/metrics/ingest", json=body,
                                      headers=self._auth(client2, token2)).status_code, 403)

    def test_prune_admin_only(self):
        client, token = self._client(self.admin)
        self.assertEqual(client.post("/api/v1/metrics/prune",
                                     headers=self._auth(client, token)).status_code, 200)
        client2, token2 = self._client(self.op)
        self.assertEqual(client2.post("/api/v1/metrics/prune",
                                      headers=self._auth(client2, token2)).status_code, 403)

    def test_trends_bad_agg_422(self):
        client, token = self._client(self.op)
        url = f"/api/v1/metrics/trends?device={self.device_id}&metric=x&agg=median"
        self.assertEqual(client.get(url, headers=self._auth(client, token)).status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
