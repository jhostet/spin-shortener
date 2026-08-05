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


def test_kv_explorer_fragment_grants_the_single_default_store():
    """Since docs/plans/kv-store-consolidation.md, the app's three named KV
    stores (links, users, analytics) are collapsed onto Spin's single
    auto-provisioned "default" store, key-prefixed by api/kvprefix.py and
    redirect/linkgate/keys.go — required because Akamai Functions allows only
    the "default" label. There is no longer any store-level separation for
    the KV explorer fragment to respect: granting "default" grants every key,
    users:user:* PBKDF2 hashes and users:session:* tokens included, with full
    CRUD. This is a deliberate, accepted local-dev-only exposure (the user
    explicitly chose it over inventing a config seam to preserve the old
    withhold) — acceptable only because this fragment is never part of a
    deployed manifest, which the sibling
    test_committed_manifest_has_no_dev_only_components test above enforces.
    """
    composed = tomllib.loads(SPIN_TOML.read_text() + KV_EXPLORER_FRAGMENT.read_text())

    assert "kv-explorer" in composed["component"]
    kv_explorer = composed["component"]["kv-explorer"]

    assert kv_explorer["key_value_stores"] == ["default"]
    assert kv_explorer["allowed_outbound_hosts"] == []

    routes = [
        trigger["route"]
        for trigger in composed["trigger"]["http"]
        if trigger.get("component") == "kv-explorer"
    ]
    assert routes == ["/internal/kv-explorer/..."]
