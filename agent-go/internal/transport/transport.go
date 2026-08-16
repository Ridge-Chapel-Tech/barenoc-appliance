// Package transport provides the mTLS HTTP client that POSTs the report to
// the appliance. Identity is the device certificate; the appliance CA root is
// the only trust anchor (InsecureSkipVerify stays false).
package transport

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/facts"
	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/version"
)

// ReportPath is the appliance endpoint the agent posts to (mTLS location).
const ReportPath = "/api/v1/device/report"

// JobsPullPath / JobsResultPath are the P1b job-transport endpoints (design §5).
const JobsPullPath = "/api/v1/device/jobs/pull"
const JobsResultPath = "/api/v1/device/jobs/result"

// ReportBody is the JSON payload POSTed to the appliance.
type ReportBody struct {
	Hostname          string   `json:"hostname"`
	OS                string   `json:"os"`
	Kernel            string   `json:"kernel"`
	MACs              []string `json:"macs"`
	IPs               []string `json:"ips"`
	UptimeS           int64    `json:"uptime_s"`
	DiskFreeGB        float64  `json:"disk_free_gb"`
	AgentVersion      string   `json:"agent_version"`
	AgentCapabilities []string `json:"agent_capabilities"`
	AdoptionMethod    string   `json:"adoption_method"`
}

// NewReportBody maps collected facts into the wire format.
func NewReportBody(f facts.Facts) ReportBody {
	return ReportBody{
		Hostname:          f.Hostname,
		OS:                f.OS,
		Kernel:            f.Kernel,
		MACs:              f.MACs,
		IPs:               f.IPs,
		UptimeS:           f.UptimeS,
		DiskFreeGB:        f.DiskFreeGB,
		AgentVersion:      version.AgentVersion,
		AgentCapabilities: []string{"report_facts"},
		AdoptionMethod:    "agent",
	}
}

// ReportURL builds the absolute report endpoint from the appliance base URL.
func ReportURL(applianceURL string) (string, error) {
	return endpointURL(applianceURL, ReportPath)
}

// JobsPullURL builds the absolute jobs/pull endpoint from the appliance base URL.
func JobsPullURL(applianceURL string) (string, error) {
	return endpointURL(applianceURL, JobsPullPath)
}

// JobsResultURL builds the absolute jobs/result endpoint from the appliance base URL.
func JobsResultURL(applianceURL string) (string, error) {
	return endpointURL(applianceURL, JobsResultPath)
}

// endpointURL joins a path onto the appliance base URL, enforcing https.
func endpointURL(applianceURL, path string) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(applianceURL), "/")
	if base == "" {
		return "", fmt.Errorf("appliance_url is empty")
	}
	if !strings.HasPrefix(base, "https://") {
		return "", fmt.Errorf("appliance_url must be https")
	}
	return base + path, nil
}

// Client wraps an mTLS-configured http.Client.
type Client struct {
	http *http.Client
}

// NewClient loads the client cert/key and CA root and builds the mTLS client.
func NewClient(certFile, keyFile, caFile string) (*Client, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load client cert/key: %w", err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read ca file: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("no certificates found in ca_file %s", caFile)
	}
	tlsCfg := &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{cert},
		RootCAs:      pool,
		// InsecureSkipVerify intentionally stays false: the agent trusts only
		// the BareNOC CA from ca_file, never the system pool.
	}
	return &Client{http: &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: tlsCfg,
		},
	}}, nil
}

// Report POSTs the body to url and returns the HTTP status and response body.
func (c *Client) Report(url string, body ReportBody) (int, []byte, error) {
	return c.PostJSON(url, body)
}

// PostJSON POSTs an arbitrary JSON payload to url (mTLS), returning status
// and body. Used by the report and by the jobs pull/result transport.
func (c *Client) PostJSON(url string, payload any) (int, []byte, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return 0, nil, fmt.Errorf("marshal payload: %w", err)
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return 0, nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "noc-agent/"+version.AgentVersion)
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, body, nil
}
