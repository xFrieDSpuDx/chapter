#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

python="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1

"$python" -m ruff check abs_kids_player tests scripts/local_release

# Adopt formatting one area at a time so functional changes stay easy to review.
"$python" -m ruff format --check \
  abs_kids_player/download_routes.py \
  abs_kids_player/library_cache.py \
  abs_kids_player/oled_bookmarks.py \
  abs_kids_player/player_control.py \
  abs_kids_player/player_state.py \
  abs_kids_player/storage.py \
  tests/test_library_cache.py \
  tests/test_player_state.py \
  tests/test_storage.py

"$python" -m unittest discover -s tests
"$python" -m compileall -q abs_kids_player tests scripts/local_release
for script in scripts/check.sh scripts/install-raspberry-pi.sh \
  tests/mock_raspberry_pi_install.sh tests/fixtures/mock-install-command; do
  bash -n "$script"
done

bash tests/mock_raspberry_pi_install.sh
