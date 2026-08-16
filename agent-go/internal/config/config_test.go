package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeTemp(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestDefaults(t *testing.T) {
	c := Default()
	if c.CertFile != "/opt/noc-agent/certs/noc-agent.crt" {
		t.Fatalf("cert_file default = %q", c.CertFile)
	}
	if c.KeyFile != "/opt/noc-agent/certs/noc-agent.key" {
		t.Fatalf("key_file default = %q", c.KeyFile)
	}
	if c.CAFile != "/opt/noc-agent/certs/ca.crt" {
		t.Fatalf("ca_file default = %q", c.CAFile)
	}
	if c.PollInterval != "30s" {
		t.Fatalf("poll_interval default = %q", c.PollInterval)
	}
	if c.LogLevel != "info" {
		t.Fatalf("log_level default = %q", c.LogLevel)
	}
}

func TestLoadValidOverlaysDefaults(t *testing.T) {
	p := writeTemp(t, `{
		"appliance_url": "https://appliance.example",
		"cn": "device-testbox",
		"poll_interval": "10s",
		"log_level": "debug"
	}`)
	c, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if c.ApplianceURL != "https://appliance.example" {
		t.Fatalf("appliance_url = %q", c.ApplianceURL)
	}
	if c.CN != "device-testbox" {
		t.Fatalf("cn = %q", c.CN)
	}
	// Unspecified fields fall back to defaults.
	if c.CertFile != "/opt/noc-agent/certs/noc-agent.crt" {
		t.Fatalf("cert_file = %q", c.CertFile)
	}
	if c.PollDuration() != 10*time.Second {
		t.Fatalf("poll duration = %v", c.PollDuration())
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "nope.json")); err == nil {
		t.Fatal("expected error for missing config file")
	}
}

func TestLoadMalformed(t *testing.T) {
	p := writeTemp(t, "{not json")
	if _, err := Load(p); err == nil {
		t.Fatal("expected error for malformed config")
	}
}

func TestValidateErrors(t *testing.T) {
	base := func() *Config {
		c := Default()
		c.ApplianceURL = "https://appliance.example"
		c.CN = "device-testbox"
		return c
	}
	cases := []struct {
		name    string
		mutate  func(*Config)
		wantErr string
	}{
		{"missing url", func(c *Config) { c.ApplianceURL = "" }, "https"},
		{"non-https url", func(c *Config) { c.ApplianceURL = "http://appliance.example" }, "https"},
		{"missing cn", func(c *Config) { c.CN = "" }, "cn"},
		{"missing cert_file", func(c *Config) { c.CertFile = "" }, "cert_file"},
		{"missing key_file", func(c *Config) { c.KeyFile = "" }, "key_file"},
		{"missing ca_file", func(c *Config) { c.CAFile = "" }, "ca_file"},
		{"bad poll_interval", func(c *Config) { c.PollInterval = "soon" }, "poll_interval"},
		{"zero poll_interval", func(c *Config) { c.PollInterval = "0s" }, "poll_interval"},
		{"bad log_level", func(c *Config) { c.LogLevel = "loud" }, "log_level"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := base()
			tc.mutate(c)
			err := c.Validate()
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("Validate() err = %v, want containing %q", err, tc.wantErr)
			}
		})
	}
}
