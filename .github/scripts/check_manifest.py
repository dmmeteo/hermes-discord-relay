#!/usr/bin/env python3
"""Validate plugin.yaml without needing a Hermes checkout.

The full contract is covered by ``tests/test_install_layout.py`` against the
real installer; this is the subset CI can assert on its own, so a manifest
typo fails the lint job rather than surviving to an install attempt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# Fields ``hermes_cli.plugins.PluginManifest`` actually carries, plus the
# ``manifest_version`` the installer reads. Anything else is dead config.
KNOWN_FIELDS = {
    "manifest_version",
    "name",
    "version",
    "description",
    "author",
    "requires_env",
    "provides_tools",
    "provides_hooks",
    "kind",
}

VALID_KINDS = {"standalone", "backend", "exclusive", "platform", "model-provider"}


def main() -> int:
    manifest_path = ROOT / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    if not isinstance(manifest, dict):
        print("plugin.yaml is not a mapping", file=sys.stderr)
        return 1

    unknown = set(manifest) - KNOWN_FIELDS
    if unknown:
        errors.append(f"unknown field(s) the loader would drop: {sorted(unknown)}")

    name = manifest.get("name")
    if not name:
        errors.append("name is required — it is the install directory name")
    elif "/" in str(name) or "\\" in str(name) or str(name).startswith("."):
        errors.append(f"name {name!r} would be rejected by the installer")

    kind = manifest.get("kind")
    if kind not in VALID_KINDS:
        errors.append(f"kind {kind!r} is not one of {sorted(VALID_KINDS)}")

    if manifest.get("manifest_version") != 1:
        errors.append("manifest_version must be 1")

    if not manifest.get("author"):
        errors.append("author is required")

    # The repository root has to be the plugin package: the installer moves the
    # whole clone into ~/.hermes/plugins/<name>/ and imports it from there.
    if not (ROOT / "__init__.py").is_file():
        errors.append("__init__.py is missing from the repository root")

    for error in errors:
        print(f"plugin.yaml: {error}", file=sys.stderr)
    if errors:
        return 1

    print(f"plugin.yaml OK ({name}, kind={kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
