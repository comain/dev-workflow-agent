#!/usr/bin/env bash
# Fast-forward the deployment checkout. Set REMOTE_URL to the repository to pull.
set -euo pipefail

repo=${REPO:-/opt/app/dev_flow_agent/repo}
remote_url=${REMOTE_URL:?set REMOTE_URL}

git -C "$repo" remote set-url origin "$remote_url"
git -C "$repo" fetch origin main
git -C "$repo" checkout main
git -C "$repo" pull --ff-only origin main
git -C "$repo" rev-parse HEAD
