package jobs

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestDeadlineExceeded(t *testing.T) {
	now := time.Date(2026, 8, 20, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		deadline string
		want     bool
		wantErr  bool
	}{
		{"", false, false},
		{"2026-08-20T13:00:00Z", false, false},
		{"2026-08-20T11:59:59Z", true, false},
		{"not-a-time", false, true},
	}
	for _, tc := range cases {
		got, err := DeadlineExceeded(tc.deadline, now)
		if (err != nil) != tc.wantErr {
			t.Fatalf("DeadlineExceeded(%q) err = %v, wantErr %v", tc.deadline, err, tc.wantErr)
		}
		if got != tc.want {
			t.Fatalf("DeadlineExceeded(%q) = %v, want %v", tc.deadline, got, tc.want)
		}
	}
}

// recordExec is an Exec that captures calls instead of running anything.
func recordExec(calls *[]string) Exec {
	return func(argv []string, _ time.Duration) (string, int, error) {
		*calls = append(*calls, strings.Join(argv, " "))
		return "ok", 0, nil
	}
}

func TestExecuteRefusesPastDeadlineWithoutRunning(t *testing.T) {
	var calls []string
	job := Job{
		JobID: "1", Action: "collect_logs", Params: map[string]any{"lines": 50},
		Deadline: "2026-08-20T11:00:00Z", Nonce: "n1",
	}
	res := Execute(job, time.Date(2026, 8, 20, 12, 0, 0, 0, time.UTC), recordExec(&calls))
	if res.OK {
		t.Fatalf("result should be failure: %+v", res)
	}
	if len(calls) != 0 {
		t.Fatalf("command must NOT run past deadline, ran: %v", calls)
	}
	if res.ExitCode != 124 {
		t.Fatalf("exit_code = %d, want 124", res.ExitCode)
	}
	if res.Nonce != "n1" || res.JobID != "1" {
		t.Fatalf("result must carry job_id + nonce: %+v", res)
	}
}

func TestExecuteRefusesUnknownAction(t *testing.T) {
	var calls []string
	res := Execute(Job{JobID: "2", Action: "install_chat_client", Nonce: "n2"},
		time.Now(), recordExec(&calls))
	if res.OK || len(calls) != 0 {
		t.Fatalf("unknown action must be refused: %+v calls=%v", res, calls)
	}
}

func TestExecuteRefusesUnconfirmedReboot(t *testing.T) {
	var calls []string
	res := Execute(Job{JobID: "3", Action: "reboot", Params: map[string]any{"confirm": false}, Nonce: "n3"},
		time.Now(), recordExec(&calls))
	if res.OK || len(calls) != 0 {
		t.Fatalf("unconfirmed reboot must be refused: %+v calls=%v", res, calls)
	}
}

func TestExecuteRunsConfirmedJob(t *testing.T) {
	var calls []string
	res := Execute(
		Job{JobID: "4", Action: "check_updates", Params: map[string]any{}, Nonce: "n4"},
		time.Now(), recordExec(&calls))
	if !res.OK {
		t.Fatalf("check_updates should succeed: %+v", res)
	}
	if len(calls) != 1 || !strings.Contains(calls[0], "/opt/noc-agent/scripts/check_updates.sh") {
		t.Fatalf("expected the multi-source check script, ran: %v", calls)
	}
	if !strings.HasPrefix(calls[0], "/usr/bin/bash ") {
		t.Fatalf("check_updates must run the script via bash (script self-escalates): %q", calls[0])
	}
	if res.ExitCode != 0 || res.DurationMs < 0 {
		t.Fatalf("bad result metadata: %+v", res)
	}
}

func TestExecuteReportFactsProducesObjectOutput(t *testing.T) {
	res := Execute(Job{JobID: "5", Action: "report_facts", Params: nil, Nonce: "n5"},
		time.Now(), nil)
	if !res.OK {
		t.Fatalf("report_facts should succeed: %+v", res)
	}
	// Output must be a JSON object (facts), not a string.
	var m map[string]any
	if err := json.Unmarshal(mustJSON(res.Output), &m); err != nil {
		t.Fatalf("report_facts output is not a JSON object: %v", err)
	}
	if _, ok := m["hostname"]; !ok {
		t.Fatalf("facts missing hostname: %v", m)
	}
}

func TestExecuteFailingCommand(t *testing.T) {
	exec := func(_ []string, _ time.Duration) (string, int, error) {
		return "boom", 1, errors.New("exit 1")
	}
	res := Execute(Job{JobID: "6", Action: "collect_logs", Params: nil, Nonce: "n6"},
		time.Now(), exec)
	if res.OK {
		t.Fatalf("failing command should yield ok=false: %+v", res)
	}
	if res.ExitCode != 1 {
		t.Fatalf("exit_code = %d, want 1", res.ExitCode)
	}
	if !strings.Contains(res.Output.(string), "boom") {
		t.Fatalf("output should contain stderr: %+v", res)
	}
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
