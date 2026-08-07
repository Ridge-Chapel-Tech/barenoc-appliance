"""Appliance device-control key — the SSH identity BareNOC uses to control
adopted devices. The API generates + stores the keypair (volumes/secrets);
the UI's Credentials modal exposes it so an operator can authorize the
public half on a device and store the private half with the device record.
"""

import os
import subprocess

CONTROL_KEY = "/opt/barenoc/volumes/secrets/device-control-key"
CONTROL_KEY_PUB = CONTROL_KEY + ".pub"


def ensure_control_key() -> dict:
    """Generate the appliance control keypair if missing; return both halves."""
    if not (os.path.exists(CONTROL_KEY) and os.path.exists(CONTROL_KEY_PUB)):
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", CONTROL_KEY, "-N", "",
             "-q", "-C", "barenoc-device-control"],
            check=True, capture_output=True)
    with open(CONTROL_KEY) as f:
        priv = f.read()
    if not priv.endswith("\n"):
        priv += "\n"  # ssh-keygen refuses keys without the trailing newline (OpenSSL 3.0)
    with open(CONTROL_KEY_PUB) as f:
        pub = f.read().strip()
    return {"public_key": pub, "private_key": priv}
