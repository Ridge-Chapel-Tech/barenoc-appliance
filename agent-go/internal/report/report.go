// Package report runs the report loop: collect facts, POST them to the
// appliance, sleep, repeat. Transient errors are logged and retried on the
// next cycle (no backoff in P1a — the fixed poll cadence is the retry).
package report

import (
	"log/slog"
	"time"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/config"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/facts"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/transport"
)

// Run blocks until stop is closed, reporting facts once per poll_interval.
func Run(cfg *config.Config, client *transport.Client, stop <-chan struct{}) {
	url, err := transport.ReportURL(cfg.ApplianceURL)
	if err != nil {
		slog.Error("invalid appliance_url", "err", err)
		return
	}
	interval := cfg.PollDuration()
	slog.Info("report loop starting", "url", url, "interval", interval)
	for {
		status, resp, err := client.Report(url, transport.NewReportBody(facts.Collect()))
		switch {
		case err != nil:
			slog.Warn("report failed; retrying next cycle", "err", err)
		case status < 200 || status >= 300:
			slog.Warn("report rejected", "status", status, "body", string(resp))
		default:
			slog.Debug("report accepted", "status", status)
		}
		select {
		case <-stop:
			return
		case <-time.After(interval):
		}
	}
}
