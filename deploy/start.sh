#!/usr/bin/env bash
set -euo pipefail

set -a
source /opt/app/dev_flow_agent/shared/.env
set +a

export HOME=/root
export PATH="/root/.opencode/bin:/opt/app/dev_flow_agent/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DFA_DATA_DIR="${DFA_DATA_DIR:-/opt/app/dev_flow_data}"
export DFA_ROOT_PATH="${DFA_ROOT_PATH:-/dev-flow-agent}"

cd /opt/app/dev_flow_agent/current
exec /opt/app/dev_flow_agent/.venv/bin/python -m dev_flow_agent dev --host 0.0.0.0 --port 8004
