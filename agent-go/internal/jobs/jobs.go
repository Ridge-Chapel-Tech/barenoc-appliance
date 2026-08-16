// Package jobs implements the P1b job loop (design §5): after each report the
// agent pulls pending jobs from the appliance, validates them against the
// embedded catalog, executes them locally (escalating only via the installed
// sudoers), and POSTs results back with the job nonce.
//
// Idempotency + offline behavior (design §5):
//   - every completed job is recorded in the local SQLite ledger keyed by
//     (job_id, nonce), so a re-offered job never re-executes;
//   - jobs pulled while offline are buffered locally and complete-or-fail
//     within their deadline on reconnect (a result computed before a POST
//     failure is re-POSTed, never re-run).
package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"time"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/actions"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/config"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/facts"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/state"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/transport"
)

// PullLimit is the number of jobs requested per pull.
const PullLimit = 10

// Job is the wire job object (design §5, server → agent).
type Job struct {
	JobID    string         `json:"job_id"`
	Action   string         `json:"action"`
	Params   map[string]any `json:"params"`
	Deadline string         `json:"deadline"`
	Nonce    string         `json:"nonce"`
}

// Result is the wire result object (design §5, agent → server).
type Result struct {
	JobID      string `json:"job_id"`
	Nonce      string `json:"nonce"`
	OK         bool   `json:"ok"`
	Output     any    `json:"output"`
	DurationMs int64  `json:"duration_ms"`
	ExitCode   int    `json:"exit_code"`
}

// Exec runs argv with a timeout and returns combined output + exit code.
type Exec func(argv []string, timeout time.Duration) (output string, exitCode int, err error)

// DefaultExec runs a command via os/exec.
func DefaultExec(argv []string, timeout time.Duration) (string, int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	code := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
		} else {
			code = -1
		}
	}
	return buf.String(), code, err
}

// DeadlineExceeded reports whether now is past an RFC3339 deadline (empty =
// no deadline).
func DeadlineExceeded(deadline string, now time.Time) (bool, error) {
	if strings.TrimSpace(deadline) == "" {
		return false, nil
	}
	t, err := time.Parse(time.RFC3339, deadline)
	if err != nil {
		return false, fmt.Errorf("bad deadline %q: %w", deadline, err)
	}
	return now.After(t), nil
}

// Execute validates + runs one job against the catalog and returns the result
// to POST back. It never panics: any refusal becomes ok=false with a message.
func Execute(j Job, now time.Time, exec Exec) Result {
	start := time.Now()
	fail := func(code int, out string) Result {
		return Result{
			JobID: j.JobID, Nonce: j.Nonce, OK: false, Output: out,
			DurationMs: time.Since(start).Milliseconds(), ExitCode: code,
		}
	}
	if err := actions.Validate(j.Action, j.Params); err != nil {
		return fail(64, err.Error())
	}
	exceeded, err := DeadlineExceeded(j.Deadline, now)
	if err != nil {
		return fail(64, err.Error())
	}
	if exceeded {
		return fail(124, "deadline exceeded — job refused")
	}
	if j.Action == actions.ReportFacts {
		f := facts.Collect()
		raw, _ := json.Marshal(f)
		return Result{
			JobID: j.JobID, Nonce: j.Nonce, OK: true, Output: json.RawMessage(raw),
			DurationMs: time.Since(start).Milliseconds(), ExitCode: 0,
		}
	}
	argv, _, err := actions.BuildCommand(j.Action, j.Params)
	if err != nil {
		return fail(64, err.Error())
	}
	spec := actions.Catalog[j.Action]
	timeout := spec.MaxDuration
	if d, derr := remaining(j.Deadline, now); derr == nil && d < timeout {
		timeout = d
	}
	out, code, runErr := exec(argv, timeout)
	if runErr != nil {
		if errors.Is(runErr, context.DeadlineExceeded) {
			return fail(124, out+"\ntimed out")
		}
		return fail(code, out+"\n"+runErr.Error())
	}
	return Result{
		JobID: j.JobID, Nonce: j.Nonce, OK: code == 0, Output: out,
		DurationMs: time.Since(start).Milliseconds(), ExitCode: code,
	}
}

func remaining(deadline string, now time.Time) (time.Duration, error) {
	if strings.TrimSpace(deadline) == "" {
		return 0, fmt.Errorf("no deadline")
	}
	t, err := time.Parse(time.RFC3339, deadline)
	if err != nil {
		return 0, err
	}
	return t.Sub(now), nil
}

// Runner drives the per-cycle job loop.
type Runner struct {
	cfg       *config.Config
	client    *transport.Client
	store     *state.Store
	pullURL   string
	resultURL string
	now       func() time.Time
	exec      Exec
}

// NewRunner builds a Runner from config + transport + local state.
func NewRunner(cfg *config.Config, client *transport.Client, store *state.Store) (*Runner, error) {
	pull, err := transport.JobsPullURL(cfg.ApplianceURL)
	if err != nil {
		return nil, err
	}
	result, err := transport.JobsResultURL(cfg.ApplianceURL)
	if err != nil {
		return nil, err
	}
	return &Runner{
		cfg: cfg, client: client, store: store,
		pullURL: pull, resultURL: result,
		now:  time.Now,
		exec: DefaultExec,
	}, nil
}

// Cycle performs one pass: pull → buffer → execute pending → report results.
func (r *Runner) Cycle() error {
	// 1. Pull (best-effort: on failure we still drain the local buffer).
	pulled, err := r.pull()
	if err != nil {
		slog.Warn("jobs pull failed; will retry next cycle", "err", err)
	} else {
		for _, j := range pulled {
			paramsJSON, _ := json.Marshal(j.Params)
			if serr := r.store.Save(state.Job{
				JobID: j.JobID, Nonce: j.Nonce, Action: j.Action,
				Params: string(paramsJSON), Deadline: j.Deadline,
			}); serr != nil {
				slog.Warn("buffer job failed", "job_id", j.JobID, "err", serr)
			}
		}
	}

	// 2. Execute buffered pending jobs (each exactly once — see ledger).
	pending, err := r.store.Pending()
	if err != nil {
		return fmt.Errorf("list pending: %w", err)
	}
	for _, j := range pending {
		done, derr := r.store.IsCompleted(j.JobID, j.Nonce)
		if derr != nil {
			slog.Warn("completed-check failed", "job_id", j.JobID, "err", derr)
			continue
		}
		if done {
			continue
		}
		res := Execute(Job{
			JobID: j.JobID, Action: j.Action, Params: parseParams(j.Params),
			Deadline: j.Deadline, Nonce: j.Nonce,
		}, r.now(), r.exec)
		resJSON, _ := json.Marshal(res)
		if merr := r.store.MarkExecuted(j.JobID, string(resJSON)); merr != nil {
			slog.Error("mark executed failed", "job_id", j.JobID, "err", merr)
		}
	}

	// 3. POST results for executed jobs; on success mark them reported.
	executed, err := r.store.Executed()
	if err != nil {
		return fmt.Errorf("list executed: %w", err)
	}
	for _, st := range executed {
		if perr := r.postResult(st); perr != nil {
			slog.Warn("result POST failed; will retry next cycle",
				"job_id", st.JobID, "err", perr)
			continue
		}
		if err := r.store.MarkReported(st.JobID); err != nil {
			slog.Warn("mark reported failed", "job_id", st.JobID, "err", err)
		}
	}
	return nil
}

func (r *Runner) pull() ([]Job, error) {
	status, body, err := r.client.PostJSON(r.pullURL, map[string]any{"limit": PullLimit})
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("pull rejected: %d %s", status, body)
	}
	var resp struct {
		Jobs []Job `json:"jobs"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("parse pull response: %w", err)
	}
	return resp.Jobs, nil
}

func (r *Runner) postResult(st state.Stored) error {
	var res Result
	if err := json.Unmarshal([]byte(st.Result), &res); err != nil {
		return fmt.Errorf("parse stored result: %w", err)
	}
	status, body, err := r.client.PostJSON(r.resultURL, res)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return fmt.Errorf("result rejected: %d %s", status, body)
	}
	return nil
}

func parseParams(raw string) map[string]any {
	var m map[string]any
	if err := json.Unmarshal([]byte(raw), &m); err != nil || m == nil {
		return map[string]any{}
	}
	return m
}
