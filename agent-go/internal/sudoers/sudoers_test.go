package sudoers

import (
	"strings"
	"testing"
)

func TestEntriesAreFullPaths(t *testing.T) {
	if len(Entries) != 5 {
		t.Fatalf("got %d entries, want 5", len(Entries))
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
		"/usr/bin/journalctl *, /sbin/reboot, /usr/bin/apt-get -s upgrade\n"
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
	// The P1b contract (BUILD_LIST §7 / task brief) is exactly these five
	// capability-gated commands. If this set changes, agent_install.sh must
	// change with it.
	want := map[string]bool{
		"/usr/bin/systemctl status *": true,
		"/usr/bin/tail *":             true,
		"/usr/bin/journalctl *":       true,
		"/sbin/reboot":                true,
		"/usr/bin/apt-get -s upgrade": true,
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
