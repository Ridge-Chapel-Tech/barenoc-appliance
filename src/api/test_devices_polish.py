#!/usr/bin/env python3
"""Cheap template/JS assertions for the devices-polish pass (08-17 feedback).

  1. Check Now is gone from the System → Updates section (auto-check on load +
     release banner is the only flow).
  3. The topology marks offline adopted UniFi gear with a distinct style.
  4. Enable control / Connect channel are gated on the device's effective
     channels (hidden when a real control channel exists).
  5. Fingerprint is not offered on Onboarded devices (discovery action only).

No browser needed — these read the shipped templates and assert the conditions
the JS uses. Wire into scripts/run_tests.sh + CI.

    docker compose exec api python3 -m unittest test_devices_polish -v
"""

import json
import os
import shutil
import subprocess
import unittest


class DevicesPolishTemplateTest(unittest.TestCase):
    @staticmethod
    def _read(name):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", name), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _extract_js_function(html, name):
        """Pull a top-level `function name(...) { ... }` out of the template.

        Brace-counts so nested object literals / callbacks stay intact (the
        functions under test have no braces inside string/regex literals).
        """
        start = html.index("function " + name + "(")
        brace = html.index("{", start)
        depth = 0
        i = brace
        while i < len(html):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    return html[start:i + 1]
            i += 1
        raise ValueError("unbalanced braces in function " + name)

    def _run_topology_builder(self, payload):
        """Execute the SHIPPED buildTopologyMermaid JS against a payload.

        Mirrors the template's exact esc() + buildTopologyMermaid() source so
        the assertion locks the real code path, not a Python re-implementation.
        Skips (not fails) when node isn't installed (e.g. a bare VM host).
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available — cannot execute the shipped JS builder")
        html = self._read("devices.html")
        script = (
            self._extract_js_function(html, "esc") + "\n"
            + self._extract_js_function(html, "buildTopologyMermaid") + "\n"
            "const chunks = [];\n"
            "process.stdin.on('data', function (d) { chunks.push(d); });\n"
            "process.stdin.on('end', function () {\n"
            "  var t = JSON.parse(chunks.join(''));\n"
            "  process.stdout.write(buildTopologyMermaid(t));\n"
            "});\n"
        )
        proc = subprocess.run(
            [node, "-e", script],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, "node failed: " + proc.stderr)
        return proc.stdout

    def test_check_now_button_removed(self):
        html = self._read("system.html")
        self.assertNotIn('>Check now<', html)          # the manual button is gone
        self.assertNotIn('function updCheck', html)    # its handler is gone too
        # …but the auto-check on load still drives the card (the only flow).
        self.assertIn("updFetch('/check'", html)
        self.assertIn('updLoad();', html)

    def test_topology_has_offline_styling(self):
        html = self._read("devices.html")
        # Offline gear gets a distinct mermaid style (red dashed outline)…
        self.assertIn('classDef off fill:#fef2f2,stroke:#dc2626', html)
        # …and the renderer applies it when the device is offline/omitted.
        self.assertIn("d.status === 'offline' || d.offline", html)
        # …via a mermaid class STATEMENT (one class per ':::' — ':::ap off' is a
        # syntax error that broke the whole topology render, fixed 08-17).
        self.assertIn("offlineNodes.push(id)", html)
        self.assertIn("class ' + offlineNodes.join(',') + ' off'", html)
        self.assertNotIn("cls += ' off'", html)

    def test_topology_renders_from_stored_source_not_dom(self):
        """#50: the cascade — a rendered SVG's textContent was fed back into
        mermaid on a later refresh ("No diagram type detected … for text:
        #topo-…{font-family:…}"). Render must come from a STORED source var,
        never from el.textContent, and a failed render must clear that source."""
        html = self._read("devices.html")
        self.assertIn("var topologySource = null;", html)
        self.assertIn("var src = topologySource;", html)
        self.assertIn("mermaid.render(id, src)", html)
        self.assertNotIn("mermaid.render(id, el.textContent)", html)
        # The error is truncated + the source cleared so the error text can
        # never be re-parsed as a diagram on a later retry/refresh.
        self.assertIn("msg.slice(0, 280)", html)
        self.assertIn("topologySource = null;", html)
        # Only ONE CDN <script> tag: callbacks are queued so a second
        # loadTopology() while the 3.3 MB bundle is still downloading can't
        # double-render (the other #50 trigger).
        self.assertIn("var mermaidCallbacks = [];", html)
        self.assertIn("mermaidCallbacks.push(cb)", html)

    def test_topology_label_escaping_hardened(self):
        """labelOf must escape every char that can terminate/alter a ["…"] node
        string: backslash + double quote (esc() already covers & < > ")."""
        html = self._read("devices.html")
        self.assertIn(r".replace(/\\/g, '&#92;')", html)
        self.assertIn(".replace(/\"/g, '&quot;')", html)
        # Edge labels are digits-only: a controller port string like "1 (2)"
        # or "1|2" breaks the |…| edge-label grammar (fuzzed against
        # mermaid@10.9.8 — |, ", ( and ) all throw).
        self.assertIn(".replace(/[^0-9]/g, '')", html)

    # Real prod topology payload (GitHub #50 reporter's device set — 6 devices,
    # 5 links, 0 clients), embedded so CI locks the exact generated graph.
    _TOPOLOGY_FIXTURE = {
        "devices": [
            {"name": "Office Wifi", "ip": "192.168.5.199", "mac": "0c:ea:14:54:c2:05",
             "model": "UAPL6", "type": "ap", "status": "online", "vendor": "Ubiquiti",
             "uplink_mac": "0c:ea:14:b5:26:7b", "uplink_remote_port": 1},
            {"name": "Ridge Chapel Tiny Farm", "ip": "100.99.121.62", "mac": "74:fa:29:17:b7:95",
             "model": "UCGMAX", "type": "gateway", "status": "online", "vendor": "Ubiquiti",
             "uplink_mac": "", "uplink_remote_port": None},
            {"name": "U6 Mesh", "ip": "192.168.1.41", "mac": "1c:6a:1b:64:dd:70",
             "model": "U6M", "type": "ap", "status": "offline", "vendor": "Ubiquiti",
             "uplink_mac": "28:70:4e:d5:f5:73", "uplink_remote_port": 1},
            {"name": "Mini Rack Switch", "ip": "192.168.1.187", "mac": "0c:ea:14:b5:26:7b",
             "model": "USL8LPB", "type": "switch", "status": "online", "vendor": "Ubiquiti",
             "uplink_mac": "74:fa:29:17:b7:95", "uplink_remote_port": 4},
            {"name": "HouseSwitch", "ip": "192.168.1.227", "mac": "28:70:4e:d5:f5:73",
             "model": "USL8LPB", "type": "switch", "status": "online", "vendor": "Ubiquiti",
             "uplink_mac": "74:fa:29:17:b7:95", "uplink_remote_port": 1},
            {"name": "U7 Outdoor", "ip": "192.168.5.144", "mac": "1c:6a:1b:95:22:f0",
             "model": "UKPW", "type": "ap", "status": "online", "vendor": "Ubiquiti",
             "uplink_mac": "0c:ea:14:b5:26:7b", "uplink_remote_port": 2},
        ],
        "clients": [],
        "links": [
            {"from": "0c:ea:14:b5:26:7b", "to": "0c:ea:14:54:c2:05", "port": 1},
            {"from": "28:70:4e:d5:f5:73", "to": "1c:6a:1b:64:dd:70", "port": 1},
            {"from": "74:fa:29:17:b7:95", "to": "0c:ea:14:b5:26:7b", "port": 4},
            {"from": "74:fa:29:17:b7:95", "to": "28:70:4e:d5:f5:73", "port": 1},
            {"from": "0c:ea:14:b5:26:7b", "to": "1c:6a:1b:95:22:f0", "port": 2},
        ],
    }

    def test_topology_generated_graph_matches_fixture(self):
        out = self._run_topology_builder(self._TOPOLOGY_FIXTURE)
        expected = "\n".join([
            "graph TD",
            'd0["Office Wifi<br/>Access Point · Ubiquiti · UAPL6"]:::ap',
            'd1["Ridge Chapel Tiny Farm<br/>Gateway · Ubiquiti · UCGMAX"]:::gw',
            'd2["U6 Mesh<br/>Access Point · Ubiquiti · U6M · offline"]:::ap',
            'd3["Mini Rack Switch<br/>Switch · Ubiquiti · USL8LPB"]:::sw',
            'd4["HouseSwitch<br/>Switch · Ubiquiti · USL8LPB"]:::sw',
            'd5["U7 Outdoor<br/>Access Point · Ubiquiti · UKPW"]:::ap',
            "class d2 off",
            "d3 -->|1| d0",
            "d4 -->|1| d2",
            "d1 -->|4| d3",
            "d1 -->|1| d4",
            "d3 -->|2| d5",
            "classDef gw fill:#eef2ff,stroke:#6366f1,stroke-width:2px",
            "classDef sw fill:#ecfeff,stroke:#0891b2,stroke-width:2px",
            "classDef ap fill:#fdf4ff,stroke:#c026d3,stroke-width:2px",
            "classDef dev fill:#f5f5f4,stroke:#78716c",
            "classDef client fill:#f0fdf4,stroke:#16a34a",
            "classDef off fill:#fef2f2,stroke:#dc2626,stroke-width:2px,stroke-dasharray:5,5",
        ])
        self.assertEqual(out, expected)

    def test_topology_adversarial_labels_escape_cleanly(self):
        """Quotes/backslashes/angle-brackets/ampersands in device+client names
        must be entity-escaped so no raw double-quote or backslash survives
        inside a ["…"] node string, and a nasty port string must be digits-only."""
        payload = {
            "devices": [{
                "name": 'Edge "quoted"\\slash (x) #1 <b>&|:|',
                "mac": "aa:bb:cc:dd:ee:01", "type": "ap",
                "vendor": "U", "model": 'M "x" \\ y', "status": "online",
            }],
            "clients": [{
                "name": "Client ] paren ( ) # & | : `tick` / <i>emoji 😀",
                "mac": "aa:bb:cc:dd:ee:02",
            }],
            "links": [{"from": "aa:bb:cc:dd:ee:01", "to": "aa:bb:cc:dd:ee:02", "port": "1 (2)|3"}],
        }
        out = self._run_topology_builder(payload)
        # No raw double quote or backslash may survive inside any node string
        # (either would let a label terminate/alter the ["…"] literal).
        for line in out.splitlines():
            if '["' in line:
                label = line.split('["', 1)[1].split('"]', 1)[0]
                self.assertNotIn('"', label)
                self.assertNotIn("\\", label)
        # …but the characters themselves must be PRESERVED as entities.
        self.assertIn("&quot;", out)
        self.assertIn("&#92;", out)
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)
        # The nasty port string is sanitized to digits so |…| stays valid.
        self.assertIn("-->|123|", out)

    def test_control_channel_gating_present(self):
        html = self._read("devices.html")
        # 'monitor' is always present, so a control channel = anything beyond it.
        self.assertIn('function hasControlChannel', html)
        self.assertIn("c !== 'monitor'", html)
        # Onboarded grid: Connect channel is hidden when controlReady.
        self.assertIn("controlReady ? '' : '<button onclick=\"openCreds", html)
        # Unclaimed list: ONE Adopt action (method chooser) — no separate
        # Enable-control button there (removed 08-17, user feedback).
        self.assertIn("showAdoptModalForDevice", html)
        self.assertNotIn('onclick="openAdopt(', html)

    def test_fingerprint_not_offered_onboarded(self):
        html = self._read("devices.html")
        # The Onboarded card no longer carries a Fingerprint action…
        self.assertNotIn('Re-run nmap fingerprint', html)
        # …while the Unclaimed list keeps a single Identify action.
        self.assertIn('>Identify</button>', html)

    def test_unclaimed_row_has_single_identify_action(self):
        """08-17: Fingerprint + Identify collapse into ONE button (no duplicate)."""
        html = self._read("devices.html")
        self.assertIn('identifyDevice(', html)            # the merged handler
        self.assertNotIn('>Fingerprint</button>', html)   # separate Fingerprint gone
        self.assertNotIn('toggleIdentify', html)          # separate toggle gone
        self.assertIn('revealedIdentifies', html)         # reveal survives re-render
        self.assertIn("/fingerprint'", html)              # merged action still scans
        self.assertIn('fingerprint/unclaimed', html)      # Identify All unchanged

    def test_adopt_chooser_reflects_decisions(self):
        """08-17: API kept + made real; Manual record relabeled + explained."""
        html = self._read("devices.html")
        # Register via API is real: login → register curl, ready to run.
        self.assertIn('Register via API', html)
        self.assertIn('/api/v1/auth/login', html)
        self.assertIn('api-register-cmd', html)
        # Manual record kept, relabeled + explained (monitor-only until channel).
        self.assertIn('Add to inventory (no credentials)', html)
        self.assertIn('Monitor-only until a channel connects', html)

    def test_existing_labels_still_present(self):
        """The relabels from #24/#31 are untouched by this pass."""
        html = self._read("devices.html")
        for label in ("Onboarded devices", "Unclaimed devices",
                      "Adopt", "Enable control", "Connect channel"):
            self.assertIn(label, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
