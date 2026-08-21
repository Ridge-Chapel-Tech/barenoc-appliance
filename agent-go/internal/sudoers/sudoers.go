// Package sudoers generates the capability-gated sudoers entry the Linux
// installer writes for the nocagent user (design §7). This is the agent-side
// mirror of agent_install.sh's sudoers line — the two MUST stay in sync, and
// the unit test below pins the exact content so a drift is caught in CI.
package sudoers

import "strings"

// User is the unprivileged account the agent daemon runs as on Linux.
const User = "nocagent"

// Entries are the exact commands the agent is allowed to escalate to.
// Fully-qualified paths only — a bare command name is a sudoers parse error.
// The per-OS package managers + the other update sources are the read-only
// multi-source check (check_updates → /opt/noc-agent/scripts/check_updates.sh)
// AND the gated apply (apply_updates → /opt/noc-agent/scripts/apply_updates.sh)
// — the SAME tools, so the tool-level grant covers both (e.g. `dnf check-update`
// for the check and `dnf -y update` for the apply). Gated at the tool level
// (never ALL); the apply is additionally confirm-gated in the catalog + the
// appliance, and the script never reboots (reboot_needed is a surfaced flag).
var Entries = []string{
	"/usr/bin/systemctl status *",
	"/usr/bin/tail *",
	"/usr/bin/journalctl *",
	"/sbin/reboot",
	"/usr/bin/apt",
	"/usr/bin/apt-get",
	"/usr/bin/dnf",
	"/usr/bin/yum",
	"/usr/bin/apk",
	"/usr/bin/zypper",
	"/usr/bin/flatpak",
	"/usr/bin/fwupdmgr",
	"/usr/bin/snap",
	"/usr/bin/rpm-ostree",
}

// Render returns the sudoers.d file contents for the given user.
func Render(user string) string {
	return user + " ALL=(root) NOPASSWD: " + strings.Join(Entries, ", ") + "\n"
}
