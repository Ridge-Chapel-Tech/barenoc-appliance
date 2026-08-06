#!/usr/bin/env python3
"""BareNOC Chat — launch script.

Usage:
    python3 barenoc_chat.py [--server https://<appliance-ip>]
"""

import argparse
import sys


def main():
    ap = argparse.ArgumentParser(description="BareNOC Chat — AIM-style client")
    ap.add_argument("--server", default=None,
                    help="BareNOC API URL, e.g. https://<appliance-ip>")
    args = ap.parse_args()

    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "tkinter is not available.\n"
            "Install Python with tkinter:\n"
            "  Linux (Debian/Ubuntu):  sudo apt install python3-tk\n"
            "  Windows/macOS:          use the installer from python.org\n"
        )
        sys.exit(1)

    # bnui imports bnapi from the same directory
    from bnui import main as run_ui
    run_ui(server=args.server)


if __name__ == "__main__":
    main()
