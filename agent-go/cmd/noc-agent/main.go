// Command noc-agent is the BareNOC endpoint agent (NOC_Agent).
//
// P1a: it loads its config, then loops forever collecting host facts and
// POSTing them to the appliance over mTLS (self-report + auto-claim with
// adoption_method="agent"). Jobs pull/execute (§5) and the update channel
// (§9) arrive in P2/P3.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/config"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/report"
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

	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sig
		slog.Info("shutdown signal received")
		close(stop)
	}()

	report.Run(cfg, client, stop)
	slog.Info("noc-agent stopped")
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
