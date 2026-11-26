# Chapter Player for Audiobookshelf

[![CI on the fork’s main branch](https://github.com/xFrieDSpuDx/chapter/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/xFrieDSpuDx/chapter/actions/workflows/ci.yml?query=branch%3Amain)

An audiobook player for Raspberry Pi with two knobs, a small OLED and a web interface you can use from your phone.

Upstream release: **[v0.3.12](https://github.com/chrismtopher/chapter/releases/tag/v0.3.12)**

Chapter Player for Audiobookshelf is an independent third-party project and is not affiliated with or supported by the Audiobookshelf project.

Chapter is a native Linux appliance app built with Python and GStreamer. It signs in to Audiobookshelf with a dedicated user account, securely stores the server-issued tokens, shows a simple two-line OLED library and playback interface, resumes from Audiobookshelf progress, and syncs progress back while listening.

The target device is a small enclosure with a 2.08 inch SH1122 SPI OLED, two KY-040 rotary encoders, a MAX98357A I2S amp, and an internal speaker.

## User guides

| I want to… | Open |
| --- | --- |
| Install Chapter on a fresh Pi | [Raspberry Pi install guide](docs/raspberry-pi-zero-2w-install.md#run-the-installer) |
| Connect Wi-Fi and play my first book | [First boot](docs/first-boot-one-sheet.md) · [Printable quick start](docs/first-boot-quick-start.pdf) |
| Browse a series, use the web player or connect a speaker | [Listening on Chapter](docs/listening-guide.md) |
| Take books offline or resume an interrupted download | [Downloads and offline listening](docs/downloads-and-offline-listening.md) |
| Assemble the player | [Visual build and wiring guide](docs/visual-build-guide.md) |
| Build, install or roll back a local package on an existing player | [Local Pi release guide](docs/local-pi-release.md) |

Check **System → System status and diagnostics → Installed software** for the build running on your Pi. The [Unreleased changelog](CHANGELOG.md#unreleased) describes this checkout’s changes since upstream v0.3.12.

## Quick Install

**New Pi.** Flash Raspberry Pi OS Lite on a Zero 2 W or WH, enable SSH and join a 2.4 GHz Wi-Fi network. Connect with the username and hostname you chose in Raspberry Pi Imager. Pick either the upstream v0.3.12 release or a committed revision available in the fork. The installer sets up the OLED, knobs, audio and web interface. [Flashing instructions](docs/raspberry-pi-zero-2w-install.md#flash-raspberry-pi-os) · [Install commands for each version](docs/raspberry-pi-zero-2w-install.md#run-the-installer)

**Existing Pi showing “Local package installed”.** Use a reviewed Debian package built for the version already running on your Pi. It includes that version for rollback. [Local upgrade and rollback](docs/local-pi-release.md)

## Hardware

Building the v5 enclosure? Follow the [illustrated build guide](docs/visual-build-guide.md) from mounting the boards to first playback. If the parts are already wired, use the [complete wiring diagram](docs/assets/chapter-v5-wiring.svg) and [power checks](docs/visual-build-guide.md#test-the-power-harness) before connecting the Pi.

Recommended build:

- Raspberry Pi Zero 2 WH, or Raspberry Pi Zero 2 W with a soldered 40-pin header
- 16 GB or larger microSD card
- Regulated 5 V power supply rated for at least 2 A; see the install guide for the Adafruit 5993's 1.5 A power-path limitation
- 2.08 inch 256x64 SH1122 SPI OLED
- Two KY-040 rotary encoder modules
- MAX98357A I2S mono amplifier breakout
- 4 ohm speaker
- 1.5 A inline fuse on the USB-C 5 V positive lead
- 3 mm warm-white power indicator LED for the lower-right front-cover hole
- Cross-connect wires for component wiring

The wiring tables are in the [install guide](docs/raspberry-pi-zero-2w-install.md#wire-the-hardware).

Purchase links for the recommended parts are in [docs/hardware-links.md](docs/hardware-links.md).

Printable case files are in [hardware/case](hardware/case).

## Setup

After installation and reboot, open the setup page from a phone or computer on the same network:

```text
http://chapter-player.local
```

If the player is not connected to Wi-Fi, it starts an open setup hotspot:

```text
Network:  Chapter-Setup
Password: none
Page:     http://10.42.0.1
```

The setup page lets you enter your Wi-Fi details and log the player into Audiobookshelf. Create a dedicated Audiobookshelf user for the player and restrict that user to the appropriate library or tags.

For a simple handout, see the [first boot one-sheet](docs/first-boot-one-sheet.md).

The local build’s illustrated quick start card is available as a [PDF](docs/first-boot-quick-start.pdf) or [JPG](docs/first-boot-quick-start.jpg).

For the local development build, use the [OLED and browser playback quick reference](docs/first-boot-one-sheet.md#control-the-same-book-from-either-place) to cancel an opening book, pause or save your place and stop.

## Controls

- Turn the Select knob to browse books.
- Click the Select knob to choose.
- Turn the Volume knob to change volume.
- Click the Volume knob to mute or unmute.
- Hold the Volume knob for 10 seconds to show the setup address.
- Hold **Select** for three seconds to open Bluetooth. Choose **Speakers**. Put a new speaker in pairing mode and select its name as soon as it appears. For a saved speaker, turn it on and select its name under **Reconnect**.
- A failed step stays on screen. Select **Retry** after waking the speaker or freeing it from another player. **Disconnect** keeps the pairing for next time.
- Turn to **Back** and click to go up one level. Hold **Select** for three seconds anywhere in Bluetooth to go straight to the library, including during a search or connection attempt.
- Parents can enable `Spoken navigation` on the Settings tab to have the player read highlighted book titles and controls aloud. It is disabled by default.
- On the standard software volume control, turn the knob for one-point steps below 10%. Press it to mute or unmute. Spoken navigation announces mute and volume changes at 10% intervals.

## Features

- Two-line SH1122 OLED library and playback UI
- Select and Volume rotary controls
- Internal speaker playback through GStreamer
- Audiobookshelf login through the local setup page
- Admin web page player controls, including play/pause and volume
- Five playback speeds from 1× to 2× in 0.25× steps
- Library browsing by title, author or series, with first-name and last-name author ordering
- Optional offline spoken navigation for book titles and controls using a tuned eSpeak NG voice
- System diagnostics, settings backup and restore, and a verified rollback for local packages
- Resume from Audiobookshelf progress
- Periodic progress sync back to Audiobookshelf
- Multi-file audiobook playback
- Continue/start-over prompt when a book has listening history
- Wi-Fi setup fallback hotspot

### Downloads and offline listening

Download a book from its Library cover, then follow the transfer in **Downloads**. Chapter prefers completed local copies by default and saves your place while offline. [Download, resume and recovery guide](docs/downloads-and-offline-listening.md)

### OLED burn-in protection

In **Settings → Display → OLED burn-in protection**, choose how long the screen stays on after the last dial press or turn. You can also set hours when the screen should be off, such as 22:00 to 06:30. The schedule uses the Pi’s local time. Both options are off by default and can be used independently.

Audio and web controls keep working with the OLED off. Press or turn either dial once to wake the screen without changing the volume or selecting anything. During scheduled hours, it stays awake for one minute after your last dial input.

### Browser player and listening queue

Open **Player** while a book is playing or paused. Below the cover you can check the playback source and saved position, scrub through the book, skip, or play and pause. In **Sleep timer**, choose **15 min**, **30 min** or **End of chapter** when a chapter marker is available. Choose **Off** to clear it.

Use the **Chapter** selector to jump to a chapter marker. In **Player → Speed**, choose **1×, 1.25×, 1.5×, 1.75× or 2×** for the current session. To save a startup speed, open **Settings → Books**. The volume slider and its + and − buttons are always visible. Below them, **Bookmarks**, **Queue** and **Session** give access to saved places, upcoming books and **Stop playback**.

Add a title from its **Book details** in Library. Open **Listening queue** from Library or Player to reorder or remove titles and play them on Chapter. Auto-advance is off by default. When enabled, a natural end of a book starts the first available queued title, or the next book in the series if there is no available queued title.

## Security

Open **Settings → Security** to set an owner username and password. Once set, controlling playback and changing settings require owner login. The owner can allow read-only visitor access to Overview, Library and Downloads. Before an owner is configured, anyone on the same network who can reach Chapter can manage it. Use a trusted home network and a dedicated Audiobookshelf account. [Owner access and recovery](docs/raspberry-pi-zero-2w-install.md#owner-access-and-password-recovery) · [Security policy](SECURITY.md)

## Development

Run the test suite with:

```bash
python3 -m unittest discover -s tests
```

The download retention test uses a disposable home and two generated audio files. To check compatibility with an older unpacked build, run this from the candidate checkout:

```bash
CHAPTER_TEST_PREVIOUS_BUILD=/path/to/previous-build python3 -m unittest discover -s tests -p test_download_retention.py
```

Run the isolated Raspberry Pi OS Lite installer simulation with:

```bash
tests/mock_raspberry_pi_install.sh
```

See [docs/appliance-design.md](docs/appliance-design.md) for hardware notes, UI behavior, and future direction.

Release history is recorded in [CHANGELOG.md](CHANGELOG.md).

## License

This project is open source under the terms of the [MIT License](LICENSE).
