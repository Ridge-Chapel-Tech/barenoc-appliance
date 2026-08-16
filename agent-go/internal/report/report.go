// Package report sends the agent's heartbeat/facts report to the appliance.
// It exposes a single-shot Once() so the caller can interleave it with the
// job loop on a shared poll cadence.
package report

import (
	"fmt"
	"log/slog"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/config"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/facts"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/transport"
)

// Once collects facts and POSTs a single report. It returns the HTTP status
// (0 when the request never completed) and any error. A 403 means the device
// was revoked — the caller should treat it as fatal (design §4).
func Once(cfg *config.Config, client *transport.Client) (int, error) {
	url, err := transport.ReportURL(cfg.ApplianceURL)
	if err != nil {
		return 0, err
	}
	status, resp, err := client.Report(url, transport.NewReportBody(facts.Collect()))
	switch {
	case err != nil:
		return status, err
	case status < 200 || status >= 300:
		return status, fmt.Errorf("report rejected: %d %s", status, string(resp))
	default:
		slog.Debug("report accepted", "status", status)
		return status, nil
	}
}
