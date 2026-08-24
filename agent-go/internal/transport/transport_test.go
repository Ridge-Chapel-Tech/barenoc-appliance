package transport

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Ridge-Chapel-Tech/BareNOC/agent-go/internal/facts"
)

func TestReportURL(t *testing.T) {
	u, err := ReportURL("https://appliance.example/")
	if err != nil {
		t.Fatal(err)
	}
	if u != "https://appliance.example/api/v1/device/report" {
		t.Fatalf("url = %q", u)
	}
	// trailing slash trimmed
	u2, err := ReportURL("https://appliance.example")
	if err != nil {
		t.Fatal(err)
	}
	if u2 != u {
		t.Fatalf("url2 = %q", u2)
	}
}

func TestReportURLErrors(t *testing.T) {
	for _, in := range []string{"", "http://appliance.example", "appliance.example"} {
		if _, err := ReportURL(in); err == nil {
			t.Fatalf("expected error for %q", in)
		}
	}
}

func TestJobsURLs(t *testing.T) {
	pull, err := JobsPullURL("https://appliance.example/")
	if err != nil {
		t.Fatal(err)
	}
	if pull != "https://appliance.example/api/v1/device/jobs/pull" {
		t.Fatalf("pull url = %q", pull)
	}
	result, err := JobsResultURL("https://appliance.example")
	if err != nil {
		t.Fatal(err)
	}
	if result != "https://appliance.example/api/v1/device/jobs/result" {
		t.Fatalf("result url = %q", result)
	}
	if _, err := JobsPullURL("http://appliance.example"); err == nil {
		t.Fatal("expected error for http appliance_url")
	}
}

func TestReportBodyShape(t *testing.T) {
	f := facts.Facts{
		Hostname: "box1", OS: "ubuntu-24.04", Kernel: "6.8.0-136",
		MACs: []string{"aa:bb:cc:dd:ee:ff"}, IPs: []string{"192.0.2.55"},
		UptimeS: 123, DiskFreeGB: 812.5,
	}
	body := NewReportBody(f)
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{"hostname", "os", "kernel", "macs", "ips", "uptime_s",
		"disk_free_gb", "agent_version", "agent_capabilities", "adoption_method"}
	for _, k := range wantKeys {
		if _, ok := m[k]; !ok {
			t.Fatalf("report body missing key %q: %s", k, raw)
		}
	}
	if m["adoption_method"] != "agent" {
		t.Fatalf("adoption_method = %v", m["adoption_method"])
	}
	if m["agent_version"] == nil || m["agent_version"] == "" {
		t.Fatalf("agent_version empty: %v", m["agent_version"])
	}
	caps, ok := m["agent_capabilities"].([]any)
	if !ok || len(caps) != 1 || caps[0] != "report_facts" {
		t.Fatalf("agent_capabilities = %v", m["agent_capabilities"])
	}
	if got := m["uptime_s"].(float64); got != 123 {
		t.Fatalf("uptime_s = %v", got)
	}
	if got := m["disk_free_gb"].(float64); got != 812.5 {
		t.Fatalf("disk_free_gb = %v", got)
	}
	if strings.Contains(string(raw), "\n") {
		t.Fatalf("unexpected newline in body: %s", raw)
	}
}
