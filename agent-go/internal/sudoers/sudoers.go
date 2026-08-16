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
var Entries = []string{
	"/usr/bin/systemctl status *",
	"/usr/bin/tail *",
	"/usr/bin/journalctl *",
	"/sbin/reboot",
	"/usr/bin/apt-get -s upgrade",
}

// Render returns the sudoers.d file contents for the given user.
func Render(user string) string {
	return user + " ALL=(root) NOPASSWD: " + strings.Join(Entries, ", ") + "\n"
}
