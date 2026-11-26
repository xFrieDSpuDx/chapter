#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOCK_WORKSPACE="$(cd "$(mktemp -d)" && pwd -P)"
MOCK_PI_ROOT="${MOCK_WORKSPACE}/raspberry-pi-os-lite"
MOCK_BIN="${MOCK_WORKSPACE}/bin"
MOCK_INSTALL_LOG="${MOCK_WORKSPACE}/commands.log"
INSTALL_DIR="${MOCK_PI_ROOT}/opt/chapter-player/git"
BOOT_CONFIG="${MOCK_PI_ROOT}/boot/firmware/config.txt"

cleanup() {
  if [[ "${KEEP_MOCK_INSTALL:-0}" == "1" ]]; then
    printf 'Mock filesystem retained at %s\n' "$MOCK_WORKSPACE"
    return
  fi
  rm -rf "$MOCK_WORKSPACE"
}
trap cleanup EXIT

fail() {
  printf 'Mock install failed: %s\n' "$*" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "missing file $1"
}

assert_contains() {
  grep -Fq "$2" "$1" || fail "$1 does not contain: $2"
}

assert_line_once() {
  local count
  count="$(grep -Fxc "$2" "$1" || true)"
  [[ "$count" == "1" ]] || fail "expected one '$2' line in $1, found $count"
}

mkdir -p "$MOCK_PI_ROOT/home/chapter" "$MOCK_BIN" "$(dirname "$BOOT_CONFIG")"
printf 'dtparam=audio=on\n' >"$BOOT_CONFIG"
: >"$MOCK_INSTALL_LOG"

for command in stat sudo apt-get raspi-config systemctl usermod chown adduser id getent git install mv sync sed tee; do
  ln -s "$PROJECT_ROOT/tests/fixtures/mock-install-command" "$MOCK_BIN/$command"
done

export MOCK_PI_ROOT MOCK_INSTALL_LOG
export MOCK_PROJECT_SOURCE="$PROJECT_ROOT"
export PATH="$MOCK_BIN:$PATH"
export CHAPTER_REPO_URL="$PROJECT_ROOT"
export CHAPTER_INSTALL_USER=chapter
export CHAPTER_INSTALL_DIR="$INSTALL_DIR"
export CHAPTER_BOOT_CONFIG="$BOOT_CONFIG"
export PYTHONPYCACHEPREFIX="${MOCK_WORKSPACE}/pycache"

run_installer() {
  bash "$PROJECT_ROOT/scripts/install-raspberry-pi.sh" --force --no-reboot "$@"
}

check_sample_library() {
  PYTHONPATH="$PROJECT_ROOT/tests" python3 -B - "$MOCK_PI_ROOT/home/chapter" "$INSTALL_DIR" "$1" <<'PY'
from pathlib import Path
import sys
from check_download_reboot import run_phase

home, build = map(Path, sys.argv[1:3])
run_phase(home, build, sys.argv[3])
PY
}

if run_installer --install-dir "$MOCK_PI_ROOT/home/chapter/app" 2>/dev/null; then
  fail "installer accepted a player-writable ancestor"
fi
run_installer
touch "$MOCK_PI_ROOT/home/chapter/.chapter-test-home"
check_sample_library seed
run_installer
check_sample_library offline

assert_line_once "$BOOT_CONFIG" "#dtparam=audio=on"
assert_line_once "$BOOT_CONFIG" "dtparam=spi=on"
assert_line_once "$BOOT_CONFIG" "dtoverlay=max98357a"
assert_line_once "$BOOT_CONFIG" "dtoverlay=i2s-mmap"

assert_file "$MOCK_PI_ROOT/etc/asound.conf"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-boot-splash.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-oled.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-setup.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-port80-forward.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-bluetooth-unblock.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-update.service"
assert_file "$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-update-recovery.service"
assert_file "$MOCK_PI_ROOT/usr/local/sbin/chapter-update-recovery"
assert_file "$MOCK_PI_ROOT/etc/NetworkManager/dnsmasq-shared.d/audiobookshelf-player-captive-portal.conf"
assert_file "$MOCK_PI_ROOT/usr/local/sbin/audiobookshelf-player-port80-proxy"
assert_file "$INSTALL_DIR/abs_kids_player/storage.py"

SETUP_SERVICE="$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-setup.service"
OLED_SERVICE="$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-oled.service"
SPLASH_SERVICE="$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-boot-splash.service"
UPDATE_SERVICE="$MOCK_PI_ROOT/etc/systemd/system/audiobookshelf-player-update.service"
assert_contains "$SETUP_SERVICE" "WorkingDirectory=$INSTALL_DIR"
assert_contains "$SETUP_SERVICE" "Environment=ABS_KIDS_PLAYER_STORAGE_OWNER=chapter"
assert_contains "$SETUP_SERVICE" "Environment=\"CHAPTER_REPO_URL=$PROJECT_ROOT\""
assert_contains "$OLED_SERVICE" "User=chapter"
assert_contains "$OLED_SERVICE" "WorkingDirectory=$INSTALL_DIR"
assert_contains "$OLED_SERVICE" "RuntimeDirectory=chapter-oled-gpio"
assert_contains "$OLED_SERVICE" "Environment=LG_WD=/run/chapter-oled-gpio"
assert_contains "$SPLASH_SERVICE" "RuntimeDirectory=chapter-splash-gpio"
assert_contains "$SPLASH_SERVICE" "Environment=LG_WD=/run/chapter-splash-gpio"
assert_contains "$OLED_SERVICE" "RuntimeDirectoryMode=0700"
assert_contains "$SPLASH_SERVICE" "RuntimeDirectoryMode=0700"
assert_contains "$UPDATE_SERVICE" "WorkingDirectory=$INSTALL_DIR"
assert_contains "$UPDATE_SERVICE" "Environment=CHAPTER_INSTALL_USER=chapter"
assert_contains "$UPDATE_SERVICE" "Environment=CHAPTER_INSTALL_DIR=$INSTALL_DIR"
assert_contains "$UPDATE_SERVICE" "Environment=\"CHAPTER_REPO_URL=$PROJECT_ROOT\""

assert_contains "$MOCK_INSTALL_LOG" "apt-get update"
assert_contains "$MOCK_INSTALL_LOG" "python3-gpiozero"
assert_contains "$MOCK_INSTALL_LOG" "gstreamer1.0-libav"
assert_contains "$MOCK_INSTALL_LOG" "espeak-ng"
assert_contains "$MOCK_INSTALL_LOG" "bluez-alsa-utils"
assert_contains "$MOCK_INSTALL_LOG" "git clone --no-checkout"
assert_contains "$MOCK_INSTALL_LOG" "/tree checkout --detach v0.3.12"
assert_contains "$MOCK_INSTALL_LOG" "git -C $INSTALL_DIR checkout --force --detach FETCH_HEAD"
assert_contains "$MOCK_INSTALL_LOG" "git -C $INSTALL_DIR fetch --no-tags -- $PROJECT_ROOT v0.3.12"
assert_contains "$MOCK_INSTALL_LOG" "systemctl enable"
assert_contains "$MOCK_INSTALL_LOG" "audiobookshelf-player-oled.service"
assert_contains "$MOCK_INSTALL_LOG" "audiobookshelf-player-update.service"
assert_contains "$MOCK_INSTALL_LOG" "mv -f -- /usr/local/sbin/chapter-update-recovery.new."
assert_contains "$MOCK_INSTALL_LOG" "audiobookshelf-player-update-recovery.service"
assert_contains "$MOCK_INSTALL_LOG" "systemctl enable --now bluealsa.service"
assert_contains "$MOCK_INSTALL_LOG" "chown -R chapter:chapter $MOCK_PI_ROOT/home/chapter/.config"
if grep -Fq "chown -R chapter:chapter $INSTALL_DIR" "$MOCK_INSTALL_LOG"; then
  fail "installer granted the player account write access to its executable code"
fi
assert_file "$INSTALL_DIR/.git/chapter-managed"

FORK_URL='https://example.test/chapter%20fork.git'
COMMIT=0123456789abcdef0123456789abcdef01234567
run_installer --repo-url "$FORK_URL" --release-ref "$COMMIT"
check_sample_library offline
check_sample_library resume
check_sample_library verify
assert_contains "$SETUP_SERVICE" 'Environment="CHAPTER_REPO_URL=https://example.test/chapter%%20fork.git"'
assert_contains "$UPDATE_SERVICE" 'Environment="CHAPTER_REPO_URL=https://example.test/chapter%%20fork.git"'
assert_contains "$MOCK_INSTALL_LOG" "git -C $INSTALL_DIR checkout --force --detach $COMMIT"
if grep -qF "fetch --no-tags -- $FORK_URL" "$MOCK_INSTALL_LOG"; then
  fail "an already fetched update commit must not fetch the release again"
fi

printf 'Mock Raspberry Pi OS Lite install passed.\n'
printf 'Validated clean install, second-run update, boot configuration, services, storage setup, and Python entry points.\n'
