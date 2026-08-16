// Package actions is the agent-side embedded action catalog (design §6).
//
// Every job is validated against this catalog before anything executes
// (defense in depth: the appliance validates too, but neither side alone can
// widen the other). Only the P1b action set is present.
package actions

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Action names in the P1b set.
const (
	CollectLogs  = "collect_logs"
	Reboot       = "reboot"
	CheckUpdates = "check_updates"
	ReportFacts  = "report_facts"
)

// Spec describes one catalog entry.
type Spec struct {
	Name        string
	Sudo        bool          // escalate via the installed sudoers entry
	Retryable   bool          // safe to re-run after a transient failure
	MaxDuration time.Duration // per-action cap (the deadline is separate)
}

// Catalog is the P1b action set (design §6). report_facts is special: it is
// handled locally (facts.Collect) and needs no command.
var Catalog = map[string]Spec{
	CollectLogs:  {Name: CollectLogs, Sudo: true, Retryable: true, MaxDuration: 30 * time.Second},
	Reboot:       {Name: Reboot, Sudo: true, Retryable: true, MaxDuration: 60 * time.Second},
	CheckUpdates: {Name: CheckUpdates, Sudo: true, Retryable: true, MaxDuration: 60 * time.Second},
	ReportFacts:  {Name: ReportFacts, Sudo: false, Retryable: true, MaxDuration: 10 * time.Second},
}

// Known reports whether name is in the catalog.
func Known(name string) bool {
	_, ok := Catalog[name]
	return ok
}

// Validate checks a job's action + params against the catalog. It returns an
// error for anything the agent must refuse to run.
func Validate(name string, params map[string]any) error {
	if !Known(name) {
		return fmt.Errorf("unknown action %q (not in the P1b agent catalog)", name)
	}
	switch name {
	case Reboot:
		// reboot is destructive: only when the appliance explicitly confirms.
		if !truthy(params["confirm"]) {
			return fmt.Errorf("reboot requires params.confirm=true")
		}
	case CollectLogs:
		if _, err := Lines(params); err != nil {
			return err
		}
	}
	return nil
}

// Lines resolves the collect_logs "lines" param (default 200, clamped 1..5000).
func Lines(params map[string]any) (int, error) {
	v, ok := params["lines"]
	if !ok || v == nil {
		return 200, nil
	}
	n, err := toInt(v)
	if err != nil {
		return 0, fmt.Errorf("collect_logs lines must be a number: %w", err)
	}
	if n < 1 || n > 5000 {
		return 0, fmt.Errorf("collect_logs lines must be 1..5000 (got %d)", n)
	}
	return n, nil
}

// BuildCommand returns the argv (including the "sudo -n" prefix when the
// action requires escalation) for the action. The command paths are the exact
// full paths in the sudoers file — capability-gating is enforced by both the
// catalog (here) and the OS (sudoers). report_facts returns no command.
func BuildCommand(name string, params map[string]any) (argv []string, sudo bool, err error) {
	if err := Validate(name, params); err != nil {
		return nil, false, err
	}
	switch name {
	case CollectLogs:
		n, _ := Lines(params)
		return []string{"sudo", "-n", "/usr/bin/journalctl", "--no-pager", "-n", strconv.Itoa(n)}, true, nil
	case Reboot:
		return []string{"sudo", "-n", "/sbin/reboot"}, true, nil
	case CheckUpdates:
		return []string{"sudo", "-n", "/usr/bin/apt-get", "-s", "upgrade"}, true, nil
	default:
		return nil, false, nil
	}
}

// truthy accepts JSON-style booleans and common string forms.
func truthy(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case string:
		switch strings.ToLower(strings.TrimSpace(x)) {
		case "true", "1", "yes", "on":
			return true
		}
	case float64:
		return x != 0
	case int:
		return x != 0
	}
	return false
}

// toInt accepts ints and JSON numbers (float64) and numeric strings.
func toInt(v any) (int, error) {
	switch x := v.(type) {
	case int:
		return x, nil
	case int64:
		return int(x), nil
	case float64:
		if x != float64(int(x)) {
			return 0, fmt.Errorf("not an integer: %v", x)
		}
		return int(x), nil
	case string:
		return strconv.Atoi(strings.TrimSpace(x))
	default:
		return 0, fmt.Errorf("cannot convert %T to int", v)
	}
}
