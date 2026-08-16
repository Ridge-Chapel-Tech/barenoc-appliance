// Package facts collects local host facts for the appliance report.
//
// Linux-first (P1a); macOS/Windows collectors arrive in P3.
package facts

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// Facts is the host self-description POSTed to the appliance.
type Facts struct {
	Hostname   string   `json:"hostname"`
	OS         string   `json:"os"`
	Kernel     string   `json:"kernel"`
	MACs       []string `json:"macs"`
	IPs        []string `json:"ips"`
	UptimeS    int64    `json:"uptime_s"`
	DiskFreeGB float64  `json:"disk_free_gb"`
}

// Collect gathers the full fact set for this host. Individual collectors fail
// soft (zero value) so a report is never blocked by one missing source.
func Collect() Facts {
	f := Facts{}
	f.Hostname, _ = os.Hostname()
	if data, err := os.ReadFile("/etc/os-release"); err == nil {
		if id, ver, err := parseOSRelease(string(data)); err == nil {
			f.OS = formatOS(id, ver)
		}
	}
	f.Kernel = kernelRelease()
	f.MACs, f.IPs = interfaces()
	if data, err := os.ReadFile("/proc/uptime"); err == nil {
		if up, err := parseUptimeSeconds(string(data)); err == nil {
			f.UptimeS = up
		}
	}
	f.DiskFreeGB = diskFreeGB("/")
	return f
}

// parseOSRelease pulls ID and VERSION_ID from /etc/os-release content.
func parseOSRelease(data string) (id, versionID string, err error) {
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, val, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		val = unquote(val)
		switch key {
		case "ID":
			id = val
		case "VERSION_ID":
			versionID = val
		}
	}
	if id == "" {
		return "", "", fmt.Errorf("os-release has no ID")
	}
	return id, versionID, nil
}

// formatOS renders "ubuntu-24.04" from ID/version (design §5 example).
func formatOS(id, versionID string) string {
	if versionID == "" {
		return id
	}
	return id + "-" + versionID
}

// unquote strips optional single/double quotes around os-release values.
func unquote(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') || (s[0] == '\'' && s[len(s)-1] == '\'') {
			return s[1 : len(s)-1]
		}
	}
	return s
}

// parseUptimeSeconds reads the first field of /proc/uptime (seconds, float).
func parseUptimeSeconds(data string) (int64, error) {
	fields := strings.Fields(data)
	if len(fields) == 0 {
		return 0, fmt.Errorf("empty uptime")
	}
	secs, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0, fmt.Errorf("bad uptime %q: %w", fields[0], err)
	}
	return int64(secs), nil
}

// kernelRelease returns uname -r via the syscall (no subprocess).
func kernelRelease() string {
	var uts syscall.Utsname
	if err := syscall.Uname(&uts); err != nil {
		return ""
	}
	return int8sToString(uts.Release[:])
}

// interfaces collects non-loopback MACs and IPv4 addresses.
func interfaces() (macs, ips []string) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, nil
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if mac := iface.HardwareAddr.String(); mac != "" {
			macs = append(macs, mac)
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipn, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipn.IP.To4()
			if ip != nil && !ip.IsLoopback() {
				ips = append(ips, ip.String())
			}
		}
	}
	return macs, ips
}

// diskFreeGB returns bytes available to unprivileged users on path, in GiB.
func diskFreeGB(path string) float64 {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0
	}
	free := uint64(st.Bavail) * uint64(st.Bsize)
	return float64(free) / (1024 * 1024 * 1024)
}

func int8sToString(c []int8) string {
	b := make([]byte, 0, len(c))
	for _, v := range c {
		if v == 0 {
			break
		}
		b = append(b, byte(v))
	}
	return string(b)
}
