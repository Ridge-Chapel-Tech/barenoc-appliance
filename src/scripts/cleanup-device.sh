#!/bin/bash
# cleanup-device.sh — remove ALL BareNOC artifacts from THIS device.
# Reverses the /onboard portal + os-setup: the barenoc control user, its
# scoped sudo entry, the appliance control key, the device certificate, the
# heartbeat + its cron entry, step-cli, and the step CA bootstrap config.
#
# Intended use: before wiping the appliance VMs for a clean-slate beta
# validation, so the dev laptop returns to its pre-onboarding state.
#
# Run on the device:  sudo bash cleanup-device.sh
set -e

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash cleanup-device.sh" >&2; exit 1; }

echo "==> Removing the heartbeat cron entry (preserving other crontab lines)"
crontab -l 2>/dev/null | grep -v barenoc-device-heartbeat | crontab - || true

echo "==> Removing heartbeat script, device cert, step-cli"
rm -f /usr/local/bin/barenoc-device-heartbeat.sh
rm -f /etc/barenoc-device.crt /etc/barenoc-device.key
rm -f /usr/local/bin/step

echo "==> Removing step CA bootstrap config (/root/.step and any user .step)"
rm -rf /root/.step
for h in /home/*; do
  if [ -d "$h/.step" ]; then
    rm -rf "$h/.step"
    echo "  removed $h/.step"
  fi
done

echo "==> Removing the barenoc sudoers entry"
rm -f /etc/sudoers.d/barenoc

echo "==> Removing the barenoc control user (home + keys)"
if id barenoc >/dev/null 2>&1; then
  userdel -r barenoc 2>/dev/null || userdel barenoc
  echo "  removed barenoc"
fi

echo "==> Scrubbing the appliance control key from authorized_keys files"
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
  [ -f "$f" ] || continue
  if grep -q 'barenoc-device-control' "$f"; then
    grep -v 'barenoc-device-control' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "  scrubbed $f"
  fi
done

echo "==> Validating sudoers"
visudo -c 2>&1 || true

echo "==> Done. Verify:"
id barenoc 2>&1 | head -1
[ -f /etc/sudoers.d/barenoc ] && echo "STILL PRESENT: /etc/sudoers.d/barenoc" || echo "ok: no barenoc sudoers"
[ -f /usr/local/bin/step ] && echo "STILL PRESENT: step-cli" || echo "ok: no step-cli"
[ -f /etc/barenoc-device.crt ] && echo "STILL PRESENT: device cert" || echo "ok: no device cert"
[ -f /usr/local/bin/barenoc-device-heartbeat.sh ] && echo "STILL PRESENT: heartbeat" || echo "ok: no heartbeat"
