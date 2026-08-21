package sudoers

import (
	"strings"
	"testing"
)

func TestEntriesAreFullPaths(t *testing.T) {
	if len(Entries) != 14 {
		t.Fatalf("got %d entries, want 14", len(Entries))
	}
	for _, e := range Entries {
		if !strings.HasPrefix(e, "/") {
			t.Fatalf("entry %q is not a fully-qualified path", e)
		}
		// The first token must be an absolute path to a real command; the
		// remainder (if any) are sudoers glob arguments.
		tok := strings.Fields(e)[0]
		if !strings.HasPrefix(tok, "/") || strings.Count(tok, "/") < 2 {
			t.Fatalf("entry %q does not start with an absolute command path", e)
		}
	}
}

func TestRenderPinsExactSudoersLine(t *testing.T) {
	want := "nocagent ALL=(root) NOPASSWD: /usr/bin/systemctl status *, /usr/bin/tail *, " +
		"/usr/bin/journalctl *, /sbin/reboot, /usr/bin/apt, /usr/bin/apt-get, /usr/bin/dnf, " +
		"/usr/bin/yum, /usr/bin/apk, /usr/bin/zypper, /usr/bin/flatpak, /usr/bin/fwupdmgr, " +
		"/usr/bin/snap, /usr/bin/rpm-ostree\n"
	if got := Render(User); got != want {
		t.Fatalf("Render(%q) = %q, want %q", User, got, want)
	}
}

func TestRenderUsesProvidedUser(t *testing.T) {
	got := Render("otheruser")
	if !strings.HasPrefix(got, "otheruser ALL=(root) NOPASSWD: ") {
		t.Fatalf("Render(otheruser) = %q", got)
	}
	if strings.Contains(got, User) {
		t.Fatalf("Render(otheruser) unexpectedly mentions %q: %q", User, got)
	}
}

func TestEntriesMatchInstallerContract(t *testing.T) {
	// The contract (BUILD_LIST §7 / task brief) is the capability-gated set:
	// the status/log/reboot controls + the per-OS package managers + the other
	// update sources. The same tool-level grant serves BOTH the read-only
	// multi-source check AND the gated apply_updates (e.g. `dnf check-update`
	// vs `dnf -y update`), so the entries did NOT need to grow for apply — the
	// apply is confirm-gated in the catalog + the appliance instead. If this
	// set changes, agent_install.sh must change with it.
	want := map[string]bool{
		"/usr/bin/systemctl status *": true,
		"/usr/bin/tail *":             true,
		"/usr/bin/journalctl *":       true,
		"/sbin/reboot":                true,
		"/usr/bin/apt":                true,
		"/usr/bin/apt-get":            true,
		"/usr/bin/dnf":                true,
		"/usr/bin/yum":                true,
		"/usr/bin/apk":                true,
		"/usr/bin/zypper":             true,
		"/usr/bin/flatpak":            true,
		"/usr/bin/fwupdmgr":           true,
		"/usr/bin/snap":               true,
		"/usr/bin/rpm-ostree":         true,
	}
	for _, e := range Entries {
		if !want[e] {
			t.Fatalf("entry %q is not in the P1b sudoers contract", e)
		}
		delete(want, e)
	}
	if len(want) != 0 {
		t.Fatalf("missing contracted sudoers entries: %v", want)
	}
}
