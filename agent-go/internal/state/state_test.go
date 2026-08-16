package state

import (
	"path/filepath"
	"testing"
	"time"
)

func openTemp(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestSavePendingAndDedupe(t *testing.T) {
	s := openTemp(t)
	j := Job{JobID: "1", Nonce: "abc", Action: "collect_logs", Params: `{"lines":10}`, Deadline: ""}
	if err := s.Save(j); err != nil {
		t.Fatal(err)
	}
	// Re-saving the same job_id (re-pull) is a no-op.
	if err := s.Save(Job{JobID: "1", Nonce: "different", Action: "reboot", Params: "{}"}); err != nil {
		t.Fatal(err)
	}
	got, err := s.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("pending = %d, want 1", len(got))
	}
	if got[0].Nonce != "abc" || got[0].Action != "collect_logs" {
		t.Fatalf("first job overwritten: %+v", got[0])
	}
}

func TestNonceDedupeLedger(t *testing.T) {
	s := openTemp(t)
	j := Job{JobID: "7", Nonce: "nonce-7", Action: "reboot", Params: `{"confirm":true}`}
	if err := s.Save(j); err != nil {
		t.Fatal(err)
	}
	done, err := s.IsCompleted("7", "nonce-7")
	if err != nil || done {
		t.Fatalf("IsCompleted before execution = %v, %v", done, err)
	}
	if err := s.MarkExecuted("7", `{"ok":true}`); err != nil {
		t.Fatal(err)
	}
	done, err = s.IsCompleted("7", "nonce-7")
	if err != nil || !done {
		t.Fatalf("IsCompleted after execution = %v, %v", done, err)
	}
	// A different nonce for the same job_id is NOT completed (nonce is part
	// of the dedupe key).
	done, err = s.IsCompleted("7", "other-nonce")
	if err != nil || done {
		t.Fatalf("IsCompleted(7, other-nonce) = %v, %v", done, err)
	}
}

func TestExecutedAndReportedFlow(t *testing.T) {
	s := openTemp(t)
	if err := s.Save(Job{JobID: "9", Nonce: "n9", Action: "check_updates", Params: "{}"}); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkExecuted("9", `{"ok":true,"output":"..."}`); err != nil {
		t.Fatal(err)
	}
	exec, err := s.Executed()
	if err != nil {
		t.Fatal(err)
	}
	if len(exec) != 1 || exec[0].Result != `{"ok":true,"output":"..."}` {
		t.Fatalf("executed = %+v", exec)
	}
	// Still not pending (executed ≠ re-run).
	pending, _ := s.Pending()
	if len(pending) != 0 {
		t.Fatalf("pending after execute = %d, want 0", len(pending))
	}
	if err := s.MarkReported("9"); err != nil {
		t.Fatal(err)
	}
	exec, _ = s.Executed()
	if len(exec) != 0 {
		t.Fatalf("executed after report = %d, want 0", len(exec))
	}
	done, _ := s.IsCompleted("9", "n9")
	if !done {
		t.Fatal("reported job should still be in the completed ledger")
	}
}

func TestPendingOrderOldestFirst(t *testing.T) {
	s := openTemp(t)
	// Force created_at ordering via sequential saves (second-granularity is
	// fine; ties break on job_id).
	_ = s.Save(Job{JobID: "a", Nonce: "1", Action: "collect_logs", Params: "{}"})
	_ = s.Save(Job{JobID: "b", Nonce: "2", Action: "check_updates", Params: "{}"})
	pending, err := s.Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 2 || pending[0].JobID != "a" || pending[1].JobID != "b" {
		t.Fatalf("pending order = %+v", pending)
	}
}

func TestPruneRemovesOldReported(t *testing.T) {
	s := openTemp(t)
	_ = s.Save(Job{JobID: "old", Nonce: "o", Action: "collect_logs", Params: "{}"})
	_ = s.MarkExecuted("old", `{"ok":true}`)
	_ = s.MarkReported("old")
	// Backdate the reported row beyond retention.
	if _, err := s.db.Exec(`UPDATE jobs SET created_at = ? WHERE job_id = 'old'`,
		time.Now().Add(-(LedgerRetention + time.Hour)).Unix()); err != nil {
		t.Fatal(err)
	}
	if err := s.Prune(LedgerRetention); err != nil {
		t.Fatal(err)
	}
	done, err := s.IsCompleted("old", "o")
	if err != nil {
		t.Fatal(err)
	}
	if done {
		t.Fatal("pruned reported job should be gone from the ledger")
	}
}

func TestPruneKeepsRecentAndPending(t *testing.T) {
	s := openTemp(t)
	_ = s.Save(Job{JobID: "recent", Nonce: "r", Action: "collect_logs", Params: "{}"})
	_ = s.MarkExecuted("recent", `{"ok":true}`)
	_ = s.MarkReported("recent")
	_ = s.Save(Job{JobID: "pending", Nonce: "p", Action: "reboot", Params: `{"confirm":true}`})
	if err := s.Prune(LedgerRetention); err != nil {
		t.Fatal(err)
	}
	done, _ := s.IsCompleted("recent", "r")
	if !done {
		t.Fatal("recent reported job should survive prune")
	}
	pending, _ := s.Pending()
	if len(pending) != 1 {
		t.Fatalf("pending after prune = %d, want 1", len(pending))
	}
}
