package actions

import "testing"

func TestKnownAndValidate(t *testing.T) {
	for _, name := range []string{CollectLogs, Reboot, CheckUpdates, ReportFacts} {
		if !Known(name) {
			t.Fatalf("%q should be known", name)
		}
	}
	if Known("install_chat_client") {
		t.Fatal("install_chat_client must NOT be in the P1b catalog")
	}
	if Known("apply_patch") {
		t.Fatal("apply_patch must NOT be in the P1b catalog")
	}
}

func TestValidateUnknownAction(t *testing.T) {
	err := Validate("install_chat_client", nil)
	if err == nil {
		t.Fatal("expected error for unknown action")
	}
}

func TestValidateRebootRequiresConfirm(t *testing.T) {
	if err := Validate(Reboot, nil); err == nil {
		t.Fatal("reboot without confirm should fail")
	}
	if err := Validate(Reboot, map[string]any{"confirm": false}); err == nil {
		t.Fatal("reboot with confirm=false should fail")
	}
	if err := Validate(Reboot, map[string]any{"confirm": true}); err != nil {
		t.Fatalf("reboot with confirm=true should pass, got %v", err)
	}
	if err := Validate(Reboot, map[string]any{"confirm": "true"}); err != nil {
		t.Fatalf("reboot with confirm='true' should pass, got %v", err)
	}
}

func TestValidateCollectLogsLines(t *testing.T) {
	if err := Validate(CollectLogs, nil); err != nil {
		t.Fatalf("collect_logs with no params should pass, got %v", err)
	}
	if err := Validate(CollectLogs, map[string]any{"lines": 200}); err != nil {
		t.Fatalf("collect_logs lines=200 should pass, got %v", err)
	}
	if err := Validate(CollectLogs, map[string]any{"lines": 0}); err == nil {
		t.Fatal("collect_logs lines=0 should fail")
	}
	if err := Validate(CollectLogs, map[string]any{"lines": 5001}); err == nil {
		t.Fatal("collect_logs lines=5001 should fail")
	}
	if err := Validate(CollectLogs, map[string]any{"lines": "abc"}); err == nil {
		t.Fatal("collect_logs lines=abc should fail")
	}
}

func TestLinesDefaultAndClamp(t *testing.T) {
	if n, err := Lines(nil); err != nil || n != 200 {
		t.Fatalf("Lines(nil) = %d, %v", n, err)
	}
	if n, _ := Lines(map[string]any{"lines": float64(50)}); n != 50 {
		t.Fatalf("Lines(50) = %d", n)
	}
	if _, err := Lines(map[string]any{"lines": 99999}); err == nil {
		t.Fatal("expected error for lines=99999")
	}
}

func TestBuildCommandUsesSudoFullPaths(t *testing.T) {
	cases := []struct {
		name   string
		params map[string]any
		want   []string
		sudo   bool
	}{
		{CollectLogs, map[string]any{"lines": 100},
			[]string{"sudo", "-n", "/usr/bin/journalctl", "--no-pager", "-n", "100"}, true},
		{Reboot, map[string]any{"confirm": true},
			[]string{"sudo", "-n", "/sbin/reboot"}, true},
		{CheckUpdates, nil,
			[]string{"sudo", "-n", "/usr/bin/apt-get", "-s", "upgrade"}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			argv, sudo, err := BuildCommand(tc.name, tc.params)
			if err != nil {
				t.Fatal(err)
			}
			if sudo != tc.sudo {
				t.Fatalf("sudo = %v, want %v", sudo, tc.sudo)
			}
			if len(argv) != len(tc.want) {
				t.Fatalf("argv = %v, want %v", argv, tc.want)
			}
			for i := range argv {
				if argv[i] != tc.want[i] {
					t.Fatalf("argv[%d] = %q, want %q", i, argv[i], tc.want[i])
				}
			}
		})
	}
}

func TestBuildCommandReportFactsHasNoCommand(t *testing.T) {
	argv, sudo, err := BuildCommand(ReportFacts, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(argv) != 0 || sudo {
		t.Fatalf("report_facts argv=%v sudo=%v, want empty/false", argv, sudo)
	}
}

func TestBuildCommandRejectsUnconfirmedReboot(t *testing.T) {
	if _, _, err := BuildCommand(Reboot, nil); err == nil {
		t.Fatal("expected error for unconfirmed reboot")
	}
}

func TestTruthy(t *testing.T) {
	cases := map[any]bool{
		true: true, false: false,
		"true": true, "TRUE": true, "1": true, "yes": true, "on": true,
		"false": false, "0": false, "": false,
		float64(1): true, float64(0): false,
		int(2): true, int(0): false,
		nil: false,
	}
	for in, want := range cases {
		if got := truthy(in); got != want {
			t.Fatalf("truthy(%v) = %v, want %v", in, got, want)
		}
	}
}
