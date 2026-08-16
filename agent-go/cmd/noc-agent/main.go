// Command noc-agent is the BareNOC endpoint agent (NOC_Agent).
//
// P1b: it loads its config, then loops forever — reporting host facts over
// mTLS (auto-claim method="agent"), then pulling/executing/results-returning
// jobs from the appliance (design §5). The update channel (§9) arrives in P3.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/config"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/jobs"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/report"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/state"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/transport"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/version"
)

func main() {
	var (
		configPath = flag.String("config", config.DefaultPath, "path to the agent config.json")
		showVer    = flag.Bool("version", false, "print the agent version and exit")
	)
	flag.Parse()

	if *showVer {
		fmt.Println(version.AgentVersion)
		return
	}

	cfg, err := config.Load(*configPath)
	if err != nil {
		slog.Error("config load failed", "err", err)
		os.Exit(1)
	}
	slog.SetDefault(newLogger(cfg.LogLevel))
	slog.Info("noc-agent starting", "version", version.AgentVersion, "config", *configPath)

	client, err := transport.NewClient(cfg.CertFile, cfg.KeyFile, cfg.CAFile)
	if err != nil {
		slog.Error("mTLS transport init failed", "err", err)
		os.Exit(1)
	}

	store, err := state.Open(cfg.StateDB)
	if err != nil {
		slog.Error("local state open failed", "err", err)
		os.Exit(1)
	}
	defer store.Close()
	if err := store.Prune(state.LedgerRetention); err != nil {
		slog.Warn("ledger prune failed", "err", err)
	}

	runner, err := jobs.NewRunner(cfg, client, store)
	if err != nil {
		slog.Error("job runner init failed", "err", err)
		os.Exit(1)
	}

	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sig
		slog.Info("shutdown signal received")
		close(stop)
	}()

	interval := cfg.PollDuration()
	slog.Info("agent loop starting", "interval", interval)
	for {
		status, rerr := report.Once(cfg, client)
		switch {
		case rerr != nil:
			slog.Warn("report failed; retrying next cycle", "err", rerr)
		case status == 403:
			// Revoked at the API layer — stop working (design §4: disable, keep
			// the cert, run no jobs).
			slog.Error("adoption revoked (403) — agent stopping")
			return
		}
		if err := runner.Cycle(); err != nil {
			slog.Warn("job cycle failed", "err", err)
		}
		select {
		case <-stop:
			slog.Info("noc-agent stopped")
			return
		case <-time.After(interval):
		}
	}
}

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	switch strings.ToLower(level) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn", "warning":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))
}
