package linkgate

import "testing"

func TestNormalizeHost_LowercasesAndStripsPort(t *testing.T) {
	if got := NormalizeHost("Trrk.IO:443"); got != "trrk.io" {
		t.Errorf("NormalizeHost = %q, want %q", got, "trrk.io")
	}
}

func TestNormalizeHost_StripsTrailingDot(t *testing.T) {
	if got := NormalizeHost("trrk.io."); got != "trrk.io" {
		t.Errorf("NormalizeHost = %q, want %q", got, "trrk.io")
	}
}

func TestNormalizeHost_HandlesBracketedIPv6WithPort(t *testing.T) {
	if got := NormalizeHost("[::1]:8080"); got != "::1" {
		t.Errorf("NormalizeHost = %q, want %q", got, "::1")
	}
}

func TestNormalizeHost_HandlesBracketedIPv6WithoutPort(t *testing.T) {
	if got := NormalizeHost("[2001:db8::1]"); got != "2001:db8::1" {
		t.Errorf("NormalizeHost = %q, want %q", got, "2001:db8::1")
	}
}

func TestNormalizeHost_EmptyIsEmpty(t *testing.T) {
	if got := NormalizeHost(""); got != "" {
		t.Errorf("NormalizeHost(\"\") = %q, want empty", got)
	}
}

func TestNormalizeHost_RejectsUnsafeBytes(t *testing.T) {
	for _, raw := range []string{"a b", "a\nb", "trrk.io/x", "trrk.io\\x"} {
		if got := NormalizeHost(raw); got != "" {
			t.Errorf("NormalizeHost(%q) = %q, want empty (unsafe byte)", raw, got)
		}
	}
}

func TestNormalizeHost_UnclosedBracketIsEmpty(t *testing.T) {
	if got := NormalizeHost("[::1"); got != "" {
		t.Errorf("NormalizeHost(\"[::1\") = %q, want empty", got)
	}
}

func TestHostFromBaseURL_ExtractsHostIgnoringSchemeAndPort(t *testing.T) {
	if got := HostFromBaseURL("https://Trrk.IO:443/"); got != "trrk.io" {
		t.Errorf("HostFromBaseURL = %q, want %q", got, "trrk.io")
	}
}

func TestHostFromBaseURL_ToleratesNoScheme(t *testing.T) {
	if got := HostFromBaseURL("trrk.io"); got != "trrk.io" {
		t.Errorf("HostFromBaseURL = %q, want %q", got, "trrk.io")
	}
}

func TestHostFromBaseURL_DropsUserinfo(t *testing.T) {
	if got := HostFromBaseURL("http://user@trrk.io"); got != "trrk.io" {
		t.Errorf("HostFromBaseURL = %q, want %q", got, "trrk.io")
	}
}

func TestHostFromBaseURL_DropsUserinfoWithAtInPassword(t *testing.T) {
	// LastIndexByte('@') is load-bearing: a userinfo password containing '@'
	// must not truncate the host at the wrong '@'.
	if got := HostFromBaseURL("http://user:p@ss@trrk.io"); got != "trrk.io" {
		t.Errorf("HostFromBaseURL = %q, want %q", got, "trrk.io")
	}
}

func TestHostAllowed_UnrestrictedLinkAllowsAnyHostIncludingEmpty(t *testing.T) {
	for _, host := range []string{"trrk.io", "anything", ""} {
		if !HostAllowed(nil, host) {
			t.Errorf("HostAllowed(nil, %q) = false, want true (unrestricted)", host)
		}
	}
	if !HostAllowed([]string{}, "trrk.io") {
		t.Error("HostAllowed([]string{}, ...) = false, want true (unrestricted)")
	}
}

func TestHostAllowed_MatchesOnHostnameIgnoringSchemeAndPort(t *testing.T) {
	// The stored entry carries https with no port; the request arrives with
	// a raw authority (a Host header never carries a scheme) on a different
	// port. Both must be ignored, matching on hostname alone.
	allowed := []string{"https://trrk.io"}
	if !HostAllowed(allowed, "trrk.io:8080") {
		t.Error("HostAllowed should ignore port on the request host")
	}
}

func TestHostAllowed_IgnoresUserinfo(t *testing.T) {
	allowed := []string{"http://user@trrk.io"}
	if !HostAllowed(allowed, "trrk.io") {
		t.Error("HostAllowed with userinfo-bearing configured entry should still match the bare host")
	}
}

func TestHostAllowed_IsCaseInsensitive(t *testing.T) {
	allowed := []string{"https://TRRK.IO"}
	if !HostAllowed(allowed, "trrk.io") {
		t.Error("HostAllowed should be case-insensitive")
	}
	allowed2 := []string{"https://trrk.io"}
	if !HostAllowed(allowed2, "TRRK.IO") {
		t.Error("HostAllowed should be case-insensitive on the request host too")
	}
}

func TestHostAllowed_StripsTrailingDot(t *testing.T) {
	allowed := []string{"https://trrk.io"}
	if !HostAllowed(allowed, "trrk.io.") {
		t.Error("HostAllowed should tolerate a trailing dot on the request host")
	}
}

func TestHostAllowed_HandlesBracketedIPv6(t *testing.T) {
	allowed := []string{"http://[::1]:3000"}
	if !HostAllowed(allowed, "[::1]:8080") {
		t.Error("HostAllowed should match bracketed IPv6 hosts regardless of port")
	}
}

func TestHostAllowed_RejectsASuffixThatIsNotAWholeLabel(t *testing.T) {
	allowed := []string{"https://trrk.io"}
	if HostAllowed(allowed, "nottrrk.io") {
		t.Error("HostAllowed(\"nottrrk.io\") against allowed \"trrk.io\" = true, want false (not a whole-label match)")
	}
}

func TestHostAllowed_RestrictedLinkRejectsUnlistedHost(t *testing.T) {
	allowed := []string{"https://trrk.io"}
	if HostAllowed(allowed, "localhost:3000") {
		t.Error("HostAllowed should reject a host not in the allowed list")
	}
}

func TestHostAllowed_RestrictedLinkRejectsEmptyHost(t *testing.T) {
	allowed := []string{"https://trrk.io"}
	if HostAllowed(allowed, "") {
		t.Error("HostAllowed should fail closed on an empty host, never fail open")
	}
}
