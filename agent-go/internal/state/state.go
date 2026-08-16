// Package state is the agent's local SQLite state (design §8): a completed-jobs
// ledger for nonce dedupe plus an offline job buffer. Everything here is
// reconstructible from a fresh pull — losing it costs a re-run, never data.
package state

import (
	"database/sql"
	"fmt"
	"time"

	_ "modernc.org/sqlite" // pure-Go SQLite driver (no cgo)
)

// Job is a buffered job awaiting execution.
type Job struct {
	JobID    string
	Nonce    string
	Action   string
	Params   string // JSON text
	Deadline string // RFC3339 or empty
}

// Stored is a job plus its local status/result.
type Stored struct {
	Job
	Status string // pending | executed | reported
	Result string // JSON text, set when executed
}

// Status values.
const (
	StatusPending  = "pending"
	StatusExecuted = "executed"
	StatusReported = "reported"
)

// LedgerRetention is how long completed (reported) jobs stay in the ledger.
const LedgerRetention = 7 * 24 * time.Hour

const schema = `
CREATE TABLE IF NOT EXISTS jobs (
	job_id     TEXT PRIMARY KEY,
	nonce      TEXT NOT NULL,
	action     TEXT NOT NULL,
	params     TEXT NOT NULL DEFAULT '{}',
	deadline   TEXT NOT NULL DEFAULT '',
	status     TEXT NOT NULL DEFAULT 'pending',
	result     TEXT NOT NULL DEFAULT '',
	created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
`

// Store is the SQLite-backed local state.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the state database at path.
func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open state db: %w", err)
	}
	// SQLite has a single writer; the agent is single-threaded here anyway,
	// but this keeps concurrent access from erroring under a race.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("init state db: %w", err)
	}
	return &Store{db: db}, nil
}

// Close closes the underlying database.
func (s *Store) Close() error { return s.db.Close() }

// Save buffers a pulled job (idempotent: a re-pull of the same job_id is a
// no-op, so a duplicated offer never re-executes).
func (s *Store) Save(j Job) error {
	_, err := s.db.Exec(
		`INSERT OR IGNORE INTO jobs(job_id, nonce, action, params, deadline, status, result, created_at)
		 VALUES(?, ?, ?, ?, ?, ?, '', ?)`,
		j.JobID, j.Nonce, j.Action, j.Params, j.Deadline, StatusPending, time.Now().Unix(),
	)
	if err != nil {
		return fmt.Errorf("save job: %w", err)
	}
	return nil
}

// Pending returns buffered jobs not yet executed, oldest first.
func (s *Store) Pending() ([]Job, error) {
	rows, err := s.db.Query(
		`SELECT job_id, nonce, action, params, deadline FROM jobs
		 WHERE status = ? ORDER BY created_at ASC, job_id ASC`, StatusPending)
	if err != nil {
		return nil, fmt.Errorf("list pending: %w", err)
	}
	defer rows.Close()
	var out []Job
	for rows.Next() {
		var j Job
		if err := rows.Scan(&j.JobID, &j.Nonce, &j.Action, &j.Params, &j.Deadline); err != nil {
			return nil, fmt.Errorf("scan pending: %w", err)
		}
		out = append(out, j)
	}
	return out, rows.Err()
}

// MarkExecuted records the job's result and flips it to executed (so a later
// crash/reconnect re-POSTs the result instead of re-running the job).
func (s *Store) MarkExecuted(jobID, resultJSON string) error {
	_, err := s.db.Exec(
		`UPDATE jobs SET status = ?, result = ? WHERE job_id = ? AND status = ?`,
		StatusExecuted, resultJSON, jobID, StatusPending)
	if err != nil {
		return fmt.Errorf("mark executed: %w", err)
	}
	return nil
}

// Executed returns jobs whose result has been computed but not yet reported.
func (s *Store) Executed() ([]Stored, error) {
	return s.byStatus(StatusExecuted)
}

// MarkReported flips an executed job to reported after a successful POST.
func (s *Store) MarkReported(jobID string) error {
	_, err := s.db.Exec(
		`UPDATE jobs SET status = ? WHERE job_id = ?`, StatusReported, jobID)
	if err != nil {
		return fmt.Errorf("mark reported: %w", err)
	}
	return nil
}

// IsCompleted reports whether (jobID, nonce) has already been executed or
// reported — the nonce-dedupe guard against re-executing a job.
func (s *Store) IsCompleted(jobID, nonce string) (bool, error) {
	var n int
	err := s.db.QueryRow(
		`SELECT COUNT(*) FROM jobs WHERE job_id = ? AND nonce = ? AND status IN (?, ?)`,
		jobID, nonce, StatusExecuted, StatusReported).Scan(&n)
	if err != nil {
		return false, fmt.Errorf("completed check: %w", err)
	}
	return n > 0, nil
}

// Prune drops reported jobs older than retention (design §5: 7-day ledger).
func (s *Store) Prune(retention time.Duration) error {
	cutoff := time.Now().Add(-retention).Unix()
	_, err := s.db.Exec(
		`DELETE FROM jobs WHERE status = ? AND created_at < ?`, StatusReported, cutoff)
	if err != nil {
		return fmt.Errorf("prune ledger: %w", err)
	}
	return nil
}

func (s *Store) byStatus(status string) ([]Stored, error) {
	rows, err := s.db.Query(
		`SELECT job_id, nonce, action, params, deadline, status, result FROM jobs
		 WHERE status = ? ORDER BY created_at ASC, job_id ASC`, status)
	if err != nil {
		return nil, fmt.Errorf("list %s: %w", status, err)
	}
	defer rows.Close()
	var out []Stored
	for rows.Next() {
		var st Stored
		if err := rows.Scan(&st.JobID, &st.Nonce, &st.Action, &st.Params,
			&st.Deadline, &st.Status, &st.Result); err != nil {
			return nil, fmt.Errorf("scan %s: %w", status, err)
		}
		out = append(out, st)
	}
	return out, rows.Err()
}
