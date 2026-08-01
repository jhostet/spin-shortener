"""Guards the local-only KV explorer convention documented in
docs/plans/kv-explorer.md.

The committed spin.toml must never mention the KV explorer: it is a
generated-manifest, gitignored, dev-only addition (dev/kv-explorer.toml +
dev/kv-explorer-up.sh), and this test is what keeps that true under CI rather
than under memory. `grep -c '^\\[component\\.' spin.toml` is NOT a usable
guard here — it returns 9, not 4, because it also matches sub-tables like
`[component.redirect.variables]` and `[component.api.build]`. Only a real
TOML parse and a set comparison on `manifest["component"]`'s keys tells the
truth.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIN_TOML = REPO_ROOT / "spin.toml"
KV_EXPLORER_FRAGMENT = REPO_ROOT / "dev" / "kv-explorer.toml"

EXPECTED_COMMITTED_COMPONENTS = {"redirect", "api", "gui", "gui-pages"}


def test_committed_manifest_has_no_dev_only_components():
    manifest = tomllib.loads(SPIN_TOML.read_text())
    actual = set(manifest["component"])
    assert actual == EXPECTED_COMMITTED_COMPONENTS, (
        f"spin.toml's component set is {actual!r}, expected exactly "
        f"{EXPECTED_COMMITTED_COMPONENTS!r}. If a new component was added "
        "on purpose, update EXPECTED_COMMITTED_COMPONENTS here. If this is "
        "the KV explorer (or another dev/*.toml fragment) leaking into the "
        "committed manifest, remove it — spin.toml must never mention it; "
        "see docs/plans/kv-explorer.md."
    )


def test_kv_explorer_fragment_grants_only_links_and_analytics():
    composed = tomllib.loads(SPIN_TOML.read_text() + KV_EXPLORER_FRAGMENT.read_text())

    assert "kv-explorer" in composed["component"]
    kv_explorer = composed["component"]["kv-explorer"]

    assert kv_explorer["key_value_stores"] == ["links", "analytics"]
    assert "users" not in kv_explorer["key_value_stores"]
    assert kv_explorer["allowed_outbound_hosts"] == []

    routes = [
        trigger["route"]
        for trigger in composed["trigger"]["http"]
        if trigger.get("component") == "kv-explorer"
    ]
    assert routes == ["/internal/kv-explorer/..."]
