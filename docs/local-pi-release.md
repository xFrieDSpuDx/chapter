# Upgrade a Pi running a local Chapter package

Use this guide when **System** shows **Local package installed**. Open **System → System status and diagnostics → Installed software** and note the build ID. On a new SD card or a player installed from Git, start with the [Raspberry Pi install guide](raspberry-pi-zero-2w-install.md#run-the-installer).

## Before you start

Get the reviewed Debian file, its SHA-256 checksum and the **candidate** and **fallback** build IDs from the person who prepared the upgrade. The fallback ID must match the build you are running. Maintainers can [build these files from a checkout](#build-an-upgrade-from-a-checkout). Chapter uses the existing services and the standard `chapter` account and storage paths on the Pi.

Stop the open book and let downloads finish. Leave the downloaded shelf as it is until reboot verification finishes. Chapter compares the book identities and track sizes before and after the switch. The candidate and fallback both use your existing settings, audio and listening positions.

## Upload and activate

**On your computer**, open the folder containing `chapter-player-local.deb` and its matching `chapter-player-local.deb.sha256`. Use your own Imager username and Pi hostname in place of the example below. You can use the Pi’s LAN address if its name does not resolve.

```bash
player=alex@chapter-player.local
scp chapter-player-local.deb chapter-player-local.deb.sha256 "${player}:" &&
  ssh "$player"
```

**On the Pi**, check the transfer before installing. The package name inside the file identifies the manager for this particular upgrade. Run these commands together so a failed checksum or installation stops activation.

```bash
cd "$HOME" &&
  sha256sum -c chapter-player-local.deb.sha256 &&
  package=$(dpkg-deb --field ./chapter-player-local.deb Package) &&
  sudo dpkg -i ./chapter-player-local.deb &&
  sudo /usr/bin/python3 -IB "/opt/chapter-player/$package/release.py" activate
```

Look for **activate passed** and the expected candidate build ID. If the manager reports a different build is selected, check **Installed software** and obtain a package built for that baseline. If the health check fails after switching, the manager attempts to restore the fallback. An interrupted switch leaves a recovery record for the next boot.

## Verify a full reboot

1. Open the web interface and check **Installed software**. On the physical OLED, turn Select and open a menu. Play a downloaded book, change the volume with the other dial, then stop playback.
2. On the Pi, run `sudo reboot`. Once it is reachable, reconnect from the same computer terminal with `ssh "$player"`.
3. On the Pi, run the following using the package you just uploaded.

```bash
cd "$HOME" &&
  package=$(dpkg-deb --field ./chapter-player-local.deb Package) &&
  sudo /usr/bin/python3 -IB "/opt/chapter-player/$package/release.py" verify-boot
```

Look for **verify-boot passed** and the same candidate ID. The check requires a new boot, the expected running build, both dials starting, a fresh OLED update through the setup service and port 80, and the original download inventory. Choose **Continue** on the book you stopped and listen for the saved passage.

If you use OLED burn-in protection, let the display go dark, then turn Volume once. The panel should wake at the same volume. To check the overnight schedule during the day, temporarily disable the idle timeout and set **Screen off from** and **Screen off until** a few minutes apart in **Settings → Display**. Leave the dials alone across both boundaries, then restore your settings.

## Roll back

On the Pi, use the **same upgrade file**. This switches to its bundled previous player and checks that the services are healthy. You can also roll back over SSH if the web interface is unavailable.

```bash
cd "$HOME" &&
  package=$(dpkg-deb --field ./chapter-player-local.deb Package) &&
  sudo /usr/bin/python3 -IB "/opt/chapter-player/$package/release.py" rollback
```

Look for **rollback passed** and the expected fallback ID. The selection survives reboot. Check **Installed software** again when you open Chapter. Rolling back the application keeps the current shelf and listening progress.

## Build an upgrade from a checkout

On a Debian build machine, use Bash, Git, Python 3.11 or newer, `dpkg-deb` (from `dpkg`) and `sha256sum` (from `coreutils`). Start in the root of the reviewed Chapter checkout containing `scripts/local_release/build.py`. The builder packages the runtime files in the working tree, including eligible untracked files. Check `git status --short` before building.

Use the **previously accepted Debian file** whose candidate matches the build ID on the target Pi. The builder takes its extracted package directory, verifies the player files against its manifest and bundles them for rollback. During activation, the OLED and setup services must both be running the selected permanent build. If you only have a fresh Pi, use the [fresh install procedure](raspberry-pi-zero-2w-install.md#run-the-installer).

Run this block in Bash. At the prompt, paste the full path to the previous package, without shell quotes. The output goes into a new release-specific directory under `~/chapter-packages/`. Keep that directory so you can use its Debian file as the baseline for the next upgrade.

```bash
(
  set -eu
  read -r -p "Path to the previously accepted .deb: " previous
  work=$(mktemp -d)
  trap 'rm -rf -- "$work"' EXIT
  previous_name=$(dpkg-deb --field "$previous" Package)
  dpkg-deb --extract "$previous" "$work/previous"
  python3 -B scripts/local_release/build.py "$work/stage" \
    "$work/previous/opt/chapter-player/$previous_name"

  package=$(awk '/^Package: / {print $2}' "$work/stage/DEBIAN/control")
  mkdir -p "$HOME/chapter-packages"
  output="$HOME/chapter-packages/$package"
  mkdir "$output"
  dpkg-deb --root-owner-group --build "$work/stage" "$output/chapter-player-local.deb"
  cd "$output"
  sha256sum chapter-player-local.deb > chapter-player-local.deb.sha256
  sha256sum -c chapter-player-local.deb.sha256
  printf 'Upgrade files are in %s\n' "$output"
)
```

Compare the printed manifest’s `fallback` with **Installed software** on the target Pi. Give the person upgrading the Pi both files and the candidate and fallback build IDs. Keep the previously accepted package as well.

## Optional responsiveness checks

If your maintainer supplied a matching `chapter-permanent.sh` helper and its original named Debian file, keep them together. On your computer, set `CHAPTER_SSH_TARGET` to the same SSH login you use for the Pi. Run the helper from its own folder. Its `install`, `verify` and `rollback` commands cover the same steps as above, with a separate reboot between install and verify.

```bash
export CHAPTER_SSH_TARGET=alex@chapter-player.local
bash chapter-permanent.sh baseline
```

Run `baseline` before installing and `bash chapter-permanent.sh dashboard` afterwards to compare seven Overview requests through port 80. Use the same playback and download activity both times. After installing, run `bash chapter-permanent.sh dials`. Wake the OLED and turn each dial one step at a time, about a second apart. The median measures the time from a single turn to the end of the OLED write.
