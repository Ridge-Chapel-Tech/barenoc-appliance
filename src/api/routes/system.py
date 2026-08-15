"""System status — BareNOC's own resources and health."""

import os
import time
from version import APP_VERSION
import json
import glob
import subprocess
from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session
from datetime import datetime
from database import get_db
from models import Device, Ticket, User
from auth import get_current_user, require_role

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _read_cpu_percent() -> float:
    """Read host CPU usage from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()[1:]
        idle = int(parts[3]) + int(parts[4])
        total = sum(int(p) for p in parts)
        time.sleep(0.3)
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()[1:]
        idle2 = int(parts[3]) + int(parts[4])
        total2 = sum(int(p) for p in parts)
        d_idle = idle2 - idle
        d_total = total2 - total
        if d_total == 0:
            return 0.0
        return round((1 - d_idle / d_total) * 100, 1)
    except Exception:
        return 0.0


def _read_mem() -> dict:
    """Read host memory from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            data = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    data[parts[0]] = int(parts[1].strip().split()[0])
        total = data.get("MemTotal", 0)
        free = data.get("MemFree", 0)
        buff = data.get("Buffers", 0)
        cache = data.get("Cached", 0)
        used = total - free - buff - cache
        pct = round((used / total) * 100, 1) if total else 0
        return {
            "total_gb": round(total / 1048576, 1),
            "used_gb": round(used / 1048576, 1),
            "percent": pct,
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "percent": 0}


def _read_disk() -> dict:
    """Read disk usage."""
    try:
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split("\n")
        parts = lines[1].split()
        return {
            "total": parts[1],
            "used": parts[2],
            "percent": parts[4].rstrip("%"),
        }
    except Exception:
        return {"total": "?", "used": "?", "percent": "?"}


def _container_status() -> list:
    """Read Docker container status via docker API socket."""
    try:
        import socket as s
        sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect("/var/run/docker.sock")
        sock.sendall(b"GET /containers/json HTTP/1.0\r\nHost: docker\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        body = data.split(b"\r\n\r\n", 1)[1]
        containers = json.loads(body)
        result = []
        for c in containers:
            names = c.get("Names", ["?"])
            state = c.get("State", "unknown")
            status = c.get("Status", state)
            result.append({
                "name": names[0].lstrip("/") if names else "?",
                "status": status,
                "up": state == "running",
            })
        return result
    except Exception:
        return []


def _uptime() -> str:
    """Read host uptime."""
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{days}d {hours}h {mins}m"
    except Exception:
        return "?"


@router.get("/status")
def system_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Comprehensive BareNOC system status."""
    # App stats — CONTROLLED devices only (onboarded = admin control: SSH
    # admin access OR adopted UniFi-managed gear OR certificate adoption).
    controlled = or_(
        Device.ssh_key_fingerprint.isnot(None),
        and_(Device.unifi_managed.is_(True), Device.claimed.is_(True)),
        and_(Device.adoption_status == "linked", Device.claimed.is_(True)),
    )
    total_devices = db.query(func.count(Device.id)).filter(controlled).scalar() or 0
    online_devices = db.query(func.count(Device.id)).filter(controlled, Device.status == "online").scalar() or 0
    total_tickets = db.query(func.count(Ticket.id)).scalar() or 0
    open_tickets = db.query(func.count(Ticket.id)).filter(
        Ticket.status.in_(["open", "in_progress", "awaiting_approval"])
    ).scalar() or 0
    total_users = db.query(func.count(User.id)).scalar() or 0
    llm_cost = db.query(func.sum(Ticket.llm_cost_usd)).scalar() or 0.0

    return {
        "host": {
            "uptime": _uptime(),
            "cpu_percent": _read_cpu_percent(),
            "memory": _read_mem(),
            "disk": _read_disk(),
        },
        "containers": _container_status(),
        "app": {
            "version": APP_VERSION,
            "total_devices": total_devices,
            "online_devices": online_devices,
            "total_tickets": total_tickets,
            "open_tickets": open_tickets,
            "total_users": total_users,
            "llm_total_cost_usd": round(llm_cost, 6),
            "db_size_mb": _db_size(),
        },
        "backups": _backup_status(),
        "timestamp": datetime.utcnow().isoformat(),
    }


def _backup_status() -> dict:
    """Backup indicators: app-data (VM) + VM snapshot/USB (host status push)."""
    app = {"count": 0, "last": None, "age_h": None, "size_mb": 0}
    try:
        files = sorted(
            glob.glob("/opt/barenoc/backups/app-backup-*.tar.gz"),
            key=os.path.getmtime,
            reverse=True,
        )
        app["count"] = len(files)
        app["size_mb"] = round(sum(os.path.getsize(f) for f in files) / 1048576, 1)
        if files:
            mt = os.path.getmtime(files[0])
            app["last"] = datetime.utcfromtimestamp(mt).isoformat() + "Z"
            app["age_h"] = round((time.time() - mt) / 3600, 1)
    except Exception:
        pass

    vm = {
        "vm_snapshot_last": None,
        "usb_present": False,
        "usb_last_backup": None,
        "updated": None,
    }
    try:
        with open("/opt/barenoc/volumes/backup_status/status.json") as f:
            vm.update(json.load(f))
    except Exception:
        pass
    return {"app": app, "vm": vm}


def _db_size() -> float:
    """Return SQLite DB size in MB."""
    try:
        path = "/opt/barenoc/volumes/db/barenoc.db"
        if os.path.exists(path):
            return round(os.path.getsize(path) / 1048576, 2)
        return 0.0
    except Exception:
        return 0.0
