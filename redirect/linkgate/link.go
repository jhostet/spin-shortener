// Package linkgate holds the redirect component's pure link-record logic —
// no spin-go-sdk imports, so it builds and tests under plain `go test`
// (importing spin-go-sdk pulls in a wit_exports.go stub only completed by
// the special `go tool componentize-go build` toolchain).
package linkgate

import "encoding/json"

type Link struct {
	Slug         string `json:"slug"`
	TargetURL    string `json:"target_url"`
	Owner        string `json:"owner"`
	Custom       bool   `json:"custom"`
	PasswordHash string `json:"password_hash"`
	Status       string `json:"status"`
	StartAt      string `json:"start_at"`
	EndAt        string `json:"end_at"`
	CreatedAt    string `json:"created_at"`
	UpdatedAt    string `json:"updated_at"`
}

// ParseLink decodes a link record fetched from KV. A JSON `null` for any
// string field (e.g. password_hash/start_at/end_at when unset) unmarshals as
// a no-op, leaving that field at its zero value "".
func ParseLink(raw []byte) (Link, error) {
	var l Link
	if err := json.Unmarshal(raw, &l); err != nil {
		return Link{}, err
	}
	return l, nil
}
