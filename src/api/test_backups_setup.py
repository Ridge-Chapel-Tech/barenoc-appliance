#!/usr/bin/env python3
"""backups-setup pass (08-17): the Backups tab surfaces remote (NAS) setup +
a guided "set up a new USB" path. Static template checks + endpoint-reuse
checks + unit tests for the new usb-setup endpoint (no browser needed).

    docker compose exec api python3 -m unittest test_backups_setup -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="backups-setup-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from routes import settings as s

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, "templates", name), encoding="utf-8") as f:
        return f.read()


class BackupsSetupTemplateTest(unittest.TestCase):
    """Static template assertions — cheap, no browser needed."""

    def test_wizard_backups_step_has_nas_form(self):
        html = _read("setup.html")
        # proto/host/share/user/pass → Connect/Disconnect, reusing the existing
        # net-mount/net-unmount endpoints (not a new implementation).
        self.assertIn('id="bk-net-proto"', html)
        self.assertIn('id="bk-net-host"', html)
        self.assertIn('id="bk-net-share"', html)
        self.assertIn('id="bk-net-user"', html)
        self.assertIn('id="bk-net-pass"', html)
        self.assertIn("function wizNetConnect", html)
        self.assertIn("function wizNetDisconnect", html)
        self.assertIn("/api/v1/settings/backups/net-mount", html)
        self.assertIn("/api/v1/settings/backups/net-unmount", html)

    def test_wizard_backups_step_has_usb_setup_entry(self):
        html = _read("setup.html")
        self.assertIn("Detect USB stick on the host", html)
        self.assertIn("function wizUsbSetupList", html)
        self.assertIn("function wizUsbSetupRun", html)
        self.assertIn("/api/v1/settings/backups/usb-setup", html)
        self.assertIn('id="bk-usb-confirm"', html)

    def test_settings_backups_tab_has_nas_form(self):
        html = _read("settings.html")
        self.assertIn('id="bk-net-proto"', html)
        self.assertIn('id="bk-net-host"', html)
        self.assertIn("function netConnect", html)
        self.assertIn("function netDisconnect", html)
        self.assertIn("/api/v1/settings/backups/net-mount", html)
        self.assertIn("/api/v1/settings/backups/net-unmount", html)

    def test_settings_backups_tab_has_usb_setup_entry(self):
        html = _read("settings.html")
        self.assertIn('id="bk-usb-setup"', html)
        self.assertIn("Detect USB stick on the host", html)
        self.assertIn("function usbSetupList", html)
        self.assertIn("function usbSetupRun", html)
        self.assertIn("/api/v1/settings/backups/usb-setup", html)
        self.assertIn('id="bk-usb-confirm"', html)


class BackupsSetupReuseTest(unittest.TestCase):
    """The NAS machinery is REUSED, not duplicated."""

    def _src(self):
        with open(os.path.join(HERE, "routes", "settings.py"), encoding="utf-8") as f:
            return f.read()

    def test_net_mount_endpoints_defined_once(self):
        src = self._src()
        self.assertEqual(src.count('@router.post("/backups/net-mount")'), 1)
        self.assertEqual(src.count('@router.post("/backups/net-unmount")'), 1)
        self.assertEqual(src.count('@router.post("/backups/usb-setup")'), 1)

    def test_usb_setup_reuses_host_run(self):
        src = self._src()
        # the new USB path drives the SAME privileged-helper mechanism as the
        # NAS mount (no second host-exec implementation).
        self.assertIn("def _host_run", src)
        self.assertIn("_host_run(script, timeout=300, chroot_host=True)", src)


class HostRunCmdTest(unittest.TestCase):
    """The privileged helper must run commands in the HOST mount namespace.
    After `nsenter -t 1 -m` the host rootfs IS /, so the old chroot-into-/host
    form (broken on fresh installs: /host is a container-only bind that
    vanishes once nsenter switches namespaces) must not come back."""

    def test_non_chroot_is_passthrough(self):
        # NAS mount/umount/status keep the plain script + nsenter they already
        # build themselves (unchanged behavior).
        script = "nsenter -t 1 -m -- mount -t cifs //nas/share /x -o ro"
        self.assertEqual(s._host_run_cmd(script, chroot_host=False), script)

    def test_chroot_host_uses_nsenter_not_chroot(self):
        cmd = s._host_run_cmd("lsblk -dnPo NAME,SIZE,MODEL,TRAN", chroot_host=True)
        self.assertIn("nsenter -t 1 -m", cmd)
        self.assertNotIn("chroot /host", cmd)   # the 08-17 fresh-install break
        self.assertIn("lsblk -dnPo NAME,SIZE,MODEL,TRAN", cmd)
        self.assertIn("PATH=/usr/local/sbin", cmd)  # host /usr/sbin resolution

    def test_chroot_host_quotes_variable_parts(self):
        cmd = s._host_run_cmd('lsblk /dev/sdb; echo "hi"', chroot_host=True)
        # the whole script is shlex.quote()d so it arrives as ONE /bin/sh -c arg
        self.assertIn("lsblk /dev/sdb; echo \"hi\"", cmd)
        self.assertTrue(cmd.endswith("'lsblk /dev/sdb; echo \"hi\"'")
                           or cmd.endswith('"lsblk /dev/sdb; echo \\"hi\\""'))


@unittest.skipUnless(
    os.environ.get("SERVICE_NAME") == "api" and os.path.exists("/var/run/docker.sock"),
    "needs the api container on a VM (docker.sock + barenoc-api image)")
class HostRunSmokeTest(unittest.TestCase):
    """End-to-end host-run smoke: proves the privileged helper reaches the
    HOST mount namespace (host hostname + host block devices). Runs on the VM
    (in-container); skipped in CI (no docker.sock)."""

    def test_host_run_reads_host_hostname(self):
        code, out = s._host_run("nsenter -t 1 -m -- cat /etc/hostname", timeout=30)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.strip())

    def test_host_run_lsblk_sees_host_devices(self):
        code, out = s._host_run("lsblk -dnPo NAME 2>/dev/null || true",
                                timeout=30, chroot_host=True)
        self.assertEqual(code, 0, out)
        self.assertIn("NAME=", out)


class BackupsSetupEndpointTest(unittest.TestCase):
    """Unit tests for POST /settings/backups/usb-setup."""

    def test_usb_setup_requires_confirm(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            s.backup_usb_setup({"action": "setup", "dev": "/dev/sdb"},
                               db=SimpleNamespace(), user=SimpleNamespace(role="admin", username="t"))

    def test_usb_setup_requires_action(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            s.backup_usb_setup({"nope": 1},
                               db=SimpleNamespace(), user=SimpleNamespace(role="admin", username="t"))

    def test_usb_setup_bad_device_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            s.backup_usb_setup({"action": "setup", "dev": "/etc/passwd", "confirm": True},
                               db=SimpleNamespace(), user=SimpleNamespace(role="admin", username="t"))

    def test_usb_list_parses_lsblk_pairs(self):
        fake = ('NAME="sda" SIZE="8.0G" MODEL="USB Flash" TRAN="usb"\n'
                'NAME="nvme0n1" SIZE="1T" MODEL="Samsung" TRAN="nvme"\n')
        with patch.object(s, "_host_run", return_value=(0, fake)):
            r = s._usb_candidates()
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(r["candidates"][0]["dev"], "/dev/sda")
        self.assertEqual(r["candidates"][0]["size"], "8.0G")
        self.assertEqual(r["candidates"][0]["model"], "USB Flash")

    def test_usb_list_no_candidates_message(self):
        fake = 'NAME="nvme0n1" SIZE="1T" MODEL="Samsung" TRAN="nvme"\n'
        with patch.object(s, "_host_run", return_value=(0, fake)):
            r = s._usb_candidates()
        self.assertEqual(r["candidates"], [])
        self.assertIn("No USB stick found", r["detail"])

    def test_usb_list_host_unreachable(self):
        # One-directional host→VM: unreachable host must return the MANUAL path
        # (exact host command + steps), not a dead-end error (08-17 fix).
        with patch.object(s, "_host_run", return_value=(1, "docker socket: boom")):
            r = s._usb_candidates()
        self.assertEqual(r["status"], "manual")
        self.assertEqual(r["candidates"], [])
        self.assertIn("manual command on the HOST", r["detail"])
        self.assertIn("setup-usb-backup.sh", r["command"])
        self.assertTrue(len(r.get("steps") or []) >= 3)

    def test_usb_setup_manual_when_script_missing(self):
        with patch.object(s, "_host_run", return_value=(1, "no such file")), \
             patch.object(s, "log_event"):
            r = s._usb_setup("/dev/sdb", db=SimpleNamespace(),
                             user=SimpleNamespace(role="admin", username="t"))
        self.assertEqual(r["status"], "manual")
        self.assertIn("setup-usb-backup.sh", r["command"])

    def test_usb_setup_ok_extracts_passphrase(self):
        out = 'stuff\nUSB backup stick READY.\nRECOVERY_PASSPHRASE="abc-123-xyz"\n'

        def fake(script, timeout=120, chroot_host=False):
            if "test -x" in script:
                return (0, "present")
            if "grep -q" in script:
                return (0, "yes")
            return (0, out)

        with patch.object(s, "_host_run", side_effect=fake), \
             patch.object(s, "log_event"):
            r = s._usb_setup("/dev/sdb", db=SimpleNamespace(),
                             user=SimpleNamespace(role="admin", username="t"))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["recovery_passphrase"], "abc-123-xyz")

    def test_usb_setup_error_returns_output(self):
        def fake(script, timeout=120, chroot_host=False):
            if "test -x" in script:
                return (0, "present")
            if "grep -q" in script:
                return (0, "yes")
            return (1, "ERROR: no stick")

        with patch.object(s, "_host_run", side_effect=fake), \
             patch.object(s, "log_event"):
            r = s._usb_setup("/dev/sdb", db=SimpleNamespace(),
                             user=SimpleNamespace(role="admin", username="t"))
        self.assertEqual(r["status"], "error")
        self.assertIn("no stick", r["output"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
