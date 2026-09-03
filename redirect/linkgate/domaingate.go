package linkgate

import "strings"

// NormalizeHost lowercases an authority and strips the port, IPv6 brackets and
// a trailing dot. Returns "" if nothing usable remains, or if the result
// carries a byte outside [a-z0-9._-] (plus ':' inside brackets, handled before
// the brackets are stripped).
//
// No regexp and no allocation beyond strings.ToLower's copy — this runs on
// EVERY redirect (via HostAllowed, which normalizes rawHost after its own
// len(allowed)==0 early return), so it must stay cheap even though the
// overwhelming majority of requests never reach the restricted branch at all.
func NormalizeHost(raw string) string {
	if raw == "" {
		return ""
	}
	host := strings.ToLower(raw)

	// Bracketed IPv6, optionally with a port after the closing bracket:
	// "[::1]:8080" -> "::1". The bracket form is the only shape that can
	// legally carry a ':' inside the host portion itself.
	if strings.HasPrefix(host, "[") {
		end := strings.IndexByte(host, ']')
		if end < 0 {
			return ""
		}
		inner := host[1:end]
		if !isValidHostBody(inner, true) {
			return ""
		}
		return inner
	}

	// Not bracketed: at most one ':' introduces a port, which is stripped.
	if idx := strings.IndexByte(host, ':'); idx >= 0 {
		host = host[:idx]
	}

	host = strings.TrimSuffix(host, ".")
	if host == "" || !isValidHostBody(host, false) {
		return ""
	}
	return host
}

// isValidHostBody reports whether s (already lowercased) contains only bytes
// a normalized host may carry: [a-z0-9._-], plus ':' when allowColon is true
// (inside IPv6 brackets only).
func isValidHostBody(s string, allowColon bool) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z':
		case c >= '0' && c <= '9':
		case c == '.' || c == '_' || c == '-':
		case allowColon && c == ':':
		default:
			return false
		}
	}
	return true
}

// HostFromBaseURL extracts the normalized host from a stored allowed_domains
// entry: "https://Trrk.IO:443/" -> "trrk.io". Tolerates an entry with no
// scheme, and drops any userinfo before the last '@' (normalize_base_url
// accepts "http://user@host", so an entry CAN carry one).
func HostFromBaseURL(entry string) string {
	rest := entry
	if idx := strings.Index(rest, "://"); idx >= 0 {
		rest = rest[idx+3:]
	}
	if idx := strings.LastIndexByte(rest, '@'); idx >= 0 {
		rest = rest[idx+1:]
	}
	// A stored base URL carries no path (normalize_base_url guarantees this),
	// but strip defensively rather than trust that invariant here too.
	if idx := strings.IndexByte(rest, '/'); idx >= 0 {
		rest = rest[:idx]
	}
	return NormalizeHost(rest)
}

// HostAllowed reports whether a request arriving with rawHost may resolve a
// link whose record carries `allowed`.
//
//   - len(allowed) == 0 -> true, unconditionally: unrestricted, today's
//     behaviour, and the ONLY branch the overwhelming majority of requests
//     take. It returns before any host work happens at all.
//   - rawHost normalizes to "" -> FALSE. Fail closed, deliberately: a
//     restriction that evaporates when the host is unknown is not a
//     restriction. This is the same direction IsWithinWindow already fails on
//     an unparseable start_at.
//   - otherwise -> membership of NormalizeHost(rawHost) in the entries' hosts.
//
// HostAllowed normalizes rawHost ITSELF, after the len(allowed)==0 early
// return, so a caller can never be wrong about whether normalization already
// happened, and an unrestricted link pays nothing for it.
func HostAllowed(allowed []string, rawHost string) bool {
	if len(allowed) == 0 {
		return true
	}

	host := NormalizeHost(rawHost)
	if host == "" {
		return false
	}

	for _, entry := range allowed {
		if HostFromBaseURL(entry) == host {
			return true
		}
	}
	return false
}
