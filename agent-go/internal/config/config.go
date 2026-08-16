// Package config loads and validates the NOC_Agent configuration.
//
// P1a uses a JSON config file (stdlib only, zero external deps). The design
// (§10) calls for config.toml; the swap to TOML is deferred — see
// agent-go/README.md.
package config

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"
)

// DefaultPath is where the Linux install places the config.
const DefaultPath = "/opt/noc-agent/config.json"

// Config is the on-disk agent configuration.
type Config struct {
	ApplianceURL string `json:"appliance_url"`
	CN           string `json:"cn"`
	CertFile     string `json:"cert_file"`
	KeyFile      string `json:"key_file"`
	CAFile       string `json:"ca_file"`
	StateDB      string `json:"state_db"`
	PollInterval string `json:"poll_interval"`
	LogLevel     string `json:"log_level"`
}

// Default returns the P1a defaults (cert paths match scripts/install_linux.sh).
func Default() *Config {
	return &Config{
		CertFile:     "/opt/noc-agent/certs/noc-agent.crt",
		KeyFile:      "/opt/noc-agent/certs/noc-agent.key",
		CAFile:       "/opt/noc-agent/certs/ca.crt",
		StateDB:      "/opt/noc-agent/state/noc-agent.db",
		PollInterval: "30s",
		LogLevel:     "info",
	}
}

// Load reads path, overlays it onto the defaults, and validates the result.
func Load(path string) (*Config, error) {
	cfg := Default()
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	cfg.ApplianceURL = strings.TrimSpace(cfg.ApplianceURL)
	cfg.CN = strings.TrimSpace(cfg.CN)
	cfg.CertFile = strings.TrimSpace(cfg.CertFile)
	cfg.KeyFile = strings.TrimSpace(cfg.KeyFile)
	cfg.CAFile = strings.TrimSpace(cfg.CAFile)
	cfg.StateDB = strings.TrimSpace(cfg.StateDB)
	cfg.PollInterval = strings.TrimSpace(cfg.PollInterval)
	cfg.LogLevel = strings.TrimSpace(cfg.LogLevel)
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Validate enforces the strict-ish rules the agent needs before it can run.
func (c *Config) Validate() error {
	u, err := url.Parse(c.ApplianceURL)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return fmt.Errorf("appliance_url must be an absolute https URL (got %q)", c.ApplianceURL)
	}
	if c.CN == "" {
		return fmt.Errorf("cn is required (the device certificate CN)")
	}
	if c.CertFile == "" {
		return fmt.Errorf("cert_file is required")
	}
	if c.KeyFile == "" {
		return fmt.Errorf("key_file is required")
	}
	if c.CAFile == "" {
		return fmt.Errorf("ca_file is required (the BareNOC CA root)")
	}
	if c.StateDB == "" {
		return fmt.Errorf("state_db is required (the local SQLite state path)")
	}
	d, err := time.ParseDuration(c.PollInterval)
	if err != nil || d <= 0 {
		return fmt.Errorf("poll_interval must be a positive duration like %q", "30s")
	}
	switch strings.ToLower(c.LogLevel) {
	case "debug", "info", "warn", "warning", "error":
	default:
		return fmt.Errorf("log_level must be one of debug|info|warn|error (got %q)", c.LogLevel)
	}
	return nil
}

// PollDuration returns the parsed poll interval, falling back to 30s.
func (c *Config) PollDuration() time.Duration {
	d, err := time.ParseDuration(c.PollInterval)
	if err != nil || d <= 0 {
		return 30 * time.Second
	}
	return d
}
