#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 OUTPUT.tar.gz" >&2
  exit 2
fi

output=$1
if [[ -e "$output" || -e "$output.sha256" ]]; then
  echo "refusing to overwrite existing release artifact: $output" >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "release snapshot requires a clean tracked worktree" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"
git archive --format=tar.gz --prefix=QwenPaw-Data/ --output="$output" HEAD

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$output" >"$output.sha256"
else
  shasum -a 256 "$output" >"$output.sha256"
fi
echo "created $output and $output.sha256"
