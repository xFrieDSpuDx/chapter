# Changelog

All notable changes to Chapter Player for Audiobookshelf are documented here.

## Unreleased

### Library and offline listening

- Download books to the Pi with their available cover art and synopsis. Downloads shows live progress, queue length and free space. Interrupted transfers resume after Wi-Fi returns or the Pi reboots when the server confirms the file is unchanged.
- Chapter prefers a completed download by default, including when opening a book from the normal library. Offline listening saves progress on the Pi and syncs it when the account is reachable again.
- Choose author first-name or last-name order, browse books in series sequence, or pin a collection or playlist. The OLED letter picker follows the selected order. The web Library adds cover browsing, search, filters for downloaded or finished books, result counts and pagination.
- Add books to a listening queue and reorder them. Optional auto-advance plays the next available queued book, or the next book in the series when the queue has none available.

### Playback and controls

- Continue on a downloaded book now picks up the latest online position, including a deliberate rewind in the Audiobookshelf browser. A newer unsynced edit on the Pi takes precedence. An open Chapter session that detects another player’s change pauses at that position, preventing stale progress from repeatedly overwriting it.
- Pending progress updates retry independently for each book or podcast episode on the active account. A rejected update for one title no longer holds up the others.
- Cancel a book while it opens or stop playback and save your place from either the OLED or the browser. A slow response from an earlier selection can no longer start that book over a newer selection.
- The browser Player shows the playback source and saved position, with chapter selection, seeking, sleep timer, bookmarks and an always-visible volume slider. Changing volume keeps the controls responsive. Added **1.25×** and **1.75×** speeds to Player and Books settings and fixed errors when changing speed during playback.
- Reduced startup delays by preparing GStreamer ahead of playback and skipping an unnecessary seek when starting at 1×. Playback diagnostics separate Audiobookshelf preparation from GStreamer startup and output.
- The volume dial offers one-point steps below 10%. The built-in speaker and Bluetooth output remember their own volume across reboots. Click feedback and spoken navigation have separate level settings.
- Bluetooth speakers appear as they are discovered. Reconnect a saved speaker or retry the failed connection step from the OLED. Holding Select returns to the library even during a search.

### Display and web interface

- Set an OLED idle timeout, scheduled screen-off hours, or both. Playback continues with the screen off. The first dial turn or press wakes it without changing volume or selection. Fixed SH1122 frame addressing that could leave the panel dark at startup or on wake.
- Added a 5% screen saver brightness and a choice of 12- or 24-hour clock. Low, nonzero Night mode settings keep the panel contrast above zero.
- Redesigned Overview, Library, Downloads, Player and Settings for phones and larger screens, with theme choices, keyboard-accessible controls and live download updates. Overview can load while Wi-Fi status refreshes in the background. The OLED reuses its library view between dial turns.
- Set an owner username and password and choose which pages visitors can read. System includes saved-position sync diagnostics and a preview of settings before restoring a backup.

### Installation and guides

- Release-managed installations use a root-owned checkout with health checks and recovery after an interrupted update. Reviewed local Debian upgrades include the previously accepted player for rollback and verify the dials, services and downloaded shelf after reboot.
- Added an illustrated assembly guide, an interactive wiring diagram with individual wire tracing and optional WAGO power connections, a printable first-boot guide, and instructions for listening offline and handing a book between Chapter and Audiobookshelf.

## [0.3.12] - 2026-10-14

- Added a configurable `1x`, `1.5x`, or `2x` playback speed setting with live updates and pitch-preserving audio.

## [0.3.11] - 2026-10-04

- Announce Volume-knob adjustments at 10% intervals when spoken navigation is enabled.
- Keep Volume-knob adjustments at or above 10% and ignore the knob's mute press while spoken navigation is enabled.

## [0.3.10] - 2026-10-03

- Install and immediately enable BlueALSA so Bluetooth speakers and headphones work on clean Raspberry Pi OS Lite installations and after software updates.

## [0.3.9] - 2026-10-02

- Shorten the administration page title and heading to `Chapter Player`.

## [0.3.8] - 2026-10-02

- Start new and restored players with an empty podcast list so parents explicitly choose every podcast.
- Preserve resolver compatibility for previously supported podcast links without adding them to the library automatically.
- Reconcile the wiring and enclosure documentation with the released hardware, including USB-C power, fuse, amplifier, speaker, and control placement guidance.
- Expand the public-release safety notes and ignore locally generated promotional images.

## [0.3.7] - 2026-10-02

- Hide the redundant successful-update detail beneath the current software version.

## [0.3.6] - 2026-10-02

- Enlarge the library alphabet jump letter without changing title, author, or series alignment.

## [0.3.5] - 2026-10-02

- Keep the administration page on rapid update polling while services restart.
- Reload the administration page automatically after the new web service reports completion.

## [0.3.4] - 2026-10-02

- Pause playback and show the OLED update warning before installation work begins.
- Wait for an OLED-ready acknowledgement before starting the updater, with a bounded fallback timeout.

## [0.3.3] - 2026-10-02

- Silence paused and muted playback completely while keeping the I2S audio path active for pop-free resume.

## [0.3.2] - 2026-10-02

- Show an `UPDATING / DO NOT POWER OFF` warning on the OLED for the full software update.
- Refresh the administration page automatically when an update completes.

## [0.3.1] - 2026-10-02

- Consolidated update availability and confirmation controls into the Software Version section.

## [0.3.0] - 2026-10-02

- Added System-tab release checks and one-button stable software updates that preserve device settings.

## [0.2.2] - 2026-10-02

- Return from the library to the current playback controls after five seconds of inactivity while audio is playing.
- Refine the lightweight eSpeak NG spoken-navigation pacing and pitch while retaining its smoother default voice.

## [0.2.1] - 2026-10-02

- Documented the trusted-local-network security model and safe deployment boundaries.
- Added GitHub Actions coverage for the test suite, syntax checks, and Raspberry Pi OS Lite installer simulation.
- Made the installer simulation portable across macOS and Linux.

## [0.2.0] - 2026-10-02

- Added a web setting to order the player library by title or by author last name.
- Added optional offline spoken navigation for highlighted book titles and controls.
- Added the Chapter color logo and refined the tabbed administration interface.
- Added CSRF protection to setup-page forms and live player controls.
- Pinned the Raspberry Pi installer to versioned releases for reproducible installs.

## [0.1.0] - 2026-10-01

Initial public release.

- Raspberry Pi Zero 2 W/WH appliance interface with SH1122 OLED and two rotary controls.
- Audiobookshelf login, library browsing, playback, resume, and progress synchronization.
- Internal MAX98357A speaker output and Bluetooth audio support.
- Podcast support, sleep timer, screensavers, and web administration.
- Automated Raspberry Pi OS Lite installer, first-boot setup hotspot, and captive portal support.
- Printable enclosure files, wiring documentation, assembly references, and parts list.
- Shared storage ownership handling and an isolated installer simulation for clean-install validation.
