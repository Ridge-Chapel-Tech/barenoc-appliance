package facts

import "testing"

func TestParseOSRelease(t *testing.T) {
	data := `NAME="Ubuntu"
VERSION="24.04.2 LTS (Noble Numbat)"
ID=ubuntu
ID_LIKE=debian
VERSION_ID="24.04"
VERSION_CODENAME=noble
`
	id, ver, err := parseOSRelease(data)
	if err != nil {
		t.Fatal(err)
	}
	if id != "ubuntu" || ver != "24.04" {
		t.Fatalf("id=%q ver=%q", id, ver)
	}
	if got := formatOS(id, ver); got != "ubuntu-24.04" {
		t.Fatalf("formatOS = %q", got)
	}
}

func TestParseOSReleaseNoVersion(t *testing.T) {
	id, ver, err := parseOSRelease("ID=alpine\n")
	if err != nil {
		t.Fatal(err)
	}
	if id != "alpine" || ver != "" {
		t.Fatalf("id=%q ver=%q", id, ver)
	}
	if got := formatOS(id, ver); got != "alpine" {
		t.Fatalf("formatOS = %q", got)
	}
}

func TestParseOSReleaseMissingID(t *testing.T) {
	if _, _, err := parseOSRelease("NAME=Nothing\n"); err == nil {
		t.Fatal("expected error for missing ID")
	}
}

func TestParseOSReleaseCommentsEmptyAndQuotes(t *testing.T) {
	id, ver, err := parseOSRelease("# a comment\n\nID='debian'\nVERSION_ID=12\n")
	if err != nil {
		t.Fatal(err)
	}
	if id != "debian" || ver != "12" {
		t.Fatalf("id=%q ver=%q", id, ver)
	}
}

func TestParseUptimeSeconds(t *testing.T) {
	up, err := parseUptimeSeconds("12345.67 98765.43\n")
	if err != nil {
		t.Fatal(err)
	}
	if up != 12345 {
		t.Fatalf("uptime = %d", up)
	}
}

func TestParseUptimeSecondsEmpty(t *testing.T) {
	if _, err := parseUptimeSeconds("\n"); err == nil {
		t.Fatal("expected error for empty uptime")
	}
}

func TestParseUptimeSecondsBad(t *testing.T) {
	if _, err := parseUptimeSeconds("not-a-number\n"); err == nil {
		t.Fatal("expected error for bad uptime")
	}
}

func TestUnquote(t *testing.T) {
	cases := map[string]string{
		`"24.04"`: "24.04",
		`'12'`:    "12",
		`noble`:   "noble",
		`"a`:      `"a`,
		``:        ``,
	}
	for in, want := range cases {
		if got := unquote(in); got != want {
			t.Fatalf("unquote(%q) = %q, want %q", in, got, want)
		}
	}
}
