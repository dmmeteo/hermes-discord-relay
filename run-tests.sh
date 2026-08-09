#!/usr/bin/env bash
# Run the test suite against the Hermes source on this machine.
#
# Override the checkout with HERMES_SOURCE_ROOT; otherwise the suite looks in
# $HERMES_HOME/hermes-agent and then ~/.hermes/hermes-agent.
#
# Runs from tests/ on purpose: the repository root is the plugin package, and
# pytest must not try to import it as a test package.
set -euo pipefail
cd "$(dirname "$0")/tests"
exec "${PYTHON:-python3}" -m pytest "$@"
