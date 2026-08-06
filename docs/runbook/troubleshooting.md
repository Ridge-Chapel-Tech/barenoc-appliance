# Troubleshooting — BareNOC Appliance

**Version:** 1.0  
**Last Updated:** 2025-07-29  
**Audience:** Internal Support / Engineering

---

## Quick Diagnostic Commands

```bash
# Check if Proxmox host is alive
ping 192.0.2.95
ssh root@192.0.2.95 "pveversion && uptime && zpool status"

# Check VM status
ssh root@192.0.2.95 "qm status 100 && qm listsnapshot 100"

# Check BareNOC services (from inside VM)
ssh barenoc@192.0.2.207 "docker compose ps && docker compose logs --tail=50"

# Check disk usage
ssh barenoc@192.0.2.207 "df -h && docker system df"
```

---

## Common Issues

### Issue: Proxmox Web UI Not Loading

| Likely Cause | Check | Fix |
|-------------|-------|-----|
| Service not running | `systemctl status pveproxy` | `systemctl restart pveproxy` |
| Port blocked | `ss -tlnp \| grep 8006` | Check firewall: `pve-firewall status` |
| Certificate expired | Check browser console | Regenerate: `pvecm updatecerts --force` |

### Issue: VM (ID 100) Not Starting

| Likely Cause | Check | Fix |
|-------------|-------|-----|
| Lock from snapshot | `qm status 100 --verbose` | Remove lock: `qm unlock 100` |
| Disk full | `zpool list` | Free space or expand ZFS |
| Corrupted config | `cat /etc/pve/qemu-server/100.conf` | Restore from backup |

### Issue: BareNOC VM Unreachable

```bash
# From Proxmox host
qm terminal 100  # or 'pct enter 100' for LXC

# Check network inside VM
ip a
ip route
ping 8.8.8.8
ping 192.0.2.1

# If network is down, restart:
systemctl restart networking
```

### Issue: Docker Services Not Running

```bash
# Inside BareNOC VM
cd /opt/barenoc
docker compose ps

# Check logs for all services
docker compose logs --tail=100

# Restart all services
docker compose down && docker compose up -d

# Check specific service
docker compose logs api --tail=50
docker compose logs worker --tail=50
```

### Issue: Database Corruption

```bash
# Check SQLite integrity
sqlite3 /opt/barenoc/volumes/db/barenoc.db "PRAGMA integrity_check;"

# If corrupted, restore from backup
gunzip -c /opt/barenoc/backups/barenoc-$(date +%Y%m%d)*.db.gz > \
    /opt/barenoc/volumes/db/barenoc.db
chown barenoc:barenoc /opt/barenoc/volumes/db/barenoc.db
docker compose restart api worker
```

### Issue: Pi Agent Jobs Stuck

```bash
# Check job directories
ls -la /opt/barenoc/jobs/incoming/
ls -la /opt/barenoc/jobs/running/
ls -la /opt/barenoc/jobs/completed/

# If jobs are stuck in 'running', move them back to incoming
mv /opt/barenoc/jobs/running/* /opt/barenoc/jobs/incoming/

# Restart agent service
systemctl restart pi-agent-runner
```

---

## Recovery Procedures

### Recover from Failed Update

```bash
# On Proxmox host
qm rollback 100 pre-update-$(date +%F)
qm start 100
```

### Recover Deleted VM

```bash
# List available backups
ls -lh /var/lib/vz/dump/
ls -lh /mnt/backup/

# Restore from latest
qmrestore /var/lib/vz/dump/vzdump-qemu-100-latest.vma.zst 100
qm start 100
```

### Recover Proxmox Root Password

1. Attach monitor + keyboard to Mini PC
2. Interrupt boot at GRUB menu
3. Append `init=/bin/bash` to kernel command line
4. Boot, remount root r/w, run `passwd`
5. Reboot

---

## When to Call for Help

| Symptom | Action |
|---------|--------|
| NanoKVM unreachable | Check NanoKVM power + Ethernet LED |
| Mini PC completely dead | Check power supply, try re-seating power connector |
| UCG Ultra factory reset itself | Re-adopt via UniFi web UI (10.0.10.1:8443) |
| Appliance won't power on at all | Check power strip, wall outlet, try different outlet |

---

## Debug Log Collection

Before escalating, gather these logs:

```bash
# On Proxmox host
journalctl -u pveproxy --no-pager -n 200 > /tmp/pveproxy.log
journalctl --no-pager -n 500 > /tmp/proxmox-syslog.log
zpool status > /tmp/zpool-status.log

# On BareNOC VM
docker compose logs --no-color --tail=500 > /tmp/barenoc-logs.txt
cat /opt/barenoc/volumes/logs/audit/latest/*.json > /tmp/audit-logs.json
sqlite3 /opt/barenoc/volumes/db/barenoc.db ".dump" > /tmp/db-dump.sql

# Send to support
tar czf /tmp/barenoc-debug-$(date +%F).tar.gz /tmp/*.log /tmp/*.json
```
