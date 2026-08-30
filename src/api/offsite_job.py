#!/usr/bin/env python3
"""Offsite-backup job entry (runs INSIDE the api container).

The host cron calls scripts/offsite_backup.sh → `docker exec barenoc-api
python3 /app/offsite_job.py`. This file lives in the api image (/app) so it can
import remote_backup and use the container's Python + cryptography + the
bind-mounted volumes. --force is for the Settings "Run now" path (also driven
directly by the API route, not via this entry).
"""

import sys

import remote_backup


def main() -> int:
    force = "--force" in sys.argv[1:]
    return remote_backup.run_offsite_backup(force=force)


if __name__ == "__main__":
    sys.exit(main())
