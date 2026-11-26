# Raspberry Pi Zero 2 W Install Guide

This guide walks through building a fresh Chapter Player for Audiobookshelf on a Raspberry Pi Zero 2 W or Zero 2 WH.

It assumes you already have an Audiobookshelf server running somewhere on your network or reachable by URL. The player stores an Audiobookshelf user token, so create a dedicated Audiobookshelf user for the player instead of using an admin account.

The software setup is handled by the installer. This guide covers the parts a new builder still needs: buying parts, flashing Raspberry Pi OS Lite, wiring the hardware, running the installer, opening the setup page, and troubleshooting.

## What To Buy

- Raspberry Pi Zero 2 WH, or a Raspberry Pi Zero 2 W plus a soldered 40-pin header.
- 16 GB or larger microSD card. Raspberry Pi OS Lite fits on less, but 16 GB or 32 GB gives you room for updates and logs.
- Good regulated 5 V power supply. Raspberry Pi recommends 2 A capacity for the Zero 2 W; 2.5 A provides additional source-side headroom for the OLED and speaker amp. See the USB-C power note below because the specified Adafruit 5993 breakout is the limiting component.
- 2.08 inch 256x64 SH1122 SPI OLED module.
- Two KY-040 rotary encoder modules.
- MAX98357A I2S mono amplifier breakout.
- 4 ohm speaker.
- 1.5 A inline fuse for the USB-C 5 V positive lead. The linked holder kit includes a suitable 1.5 A fast-blow 5x20 mm fuse.
- 3 mm warm-white power indicator LED for the lower-right front-cover hole. The build uses the 5-6 V [Dioramo 13240](https://dioramo.com/products/13240).
- Cross-connect wires or soldered wiring, plus an enclosure. The build uses [Amazon ASIN B01EV70C78](https://www.amazon.com/dp/B01EV70C78?th=1) for component wiring.

For links to the recommended parts, see the [hardware links](hardware-links.md).

The Pi Zero 2 W Wi-Fi radio is 2.4 GHz only. Use a 2.4 GHz network during setup.

## Flash Raspberry Pi OS

1. Install Raspberry Pi Imager on your computer.
2. Insert the microSD card.
3. In Imager, choose the Pi model and select **Raspberry Pi OS Lite**.
4. Open OS customisation before writing the card.
5. Choose a hostname. This guide uses `chapter-player`.
6. Choose your own SSH login username. Chapter’s services normally run under a separate account named `chapter`.
7. Set a password you will remember.
8. Configure your 2.4 GHz Wi-Fi network and country.
9. Enable SSH.
10. Write the card, insert it into the Pi, and power the Pi.

Wait a few minutes for the first boot. On your computer, replace `alex` and `chapter-player` with the username and hostname you chose in Imager, then connect.

```bash
ssh alex@chapter-player.local
```

If the name does not resolve, use the Pi’s LAN address from your router in place of `chapter-player.local`. Use the same hostname or address when you open the web interface later.

## Run The Installer

These two routes are for a **fresh Pi**. Run one of them in your SSH session. You need internet access to fetch the software and system packages, and a Pi login that can use `sudo`. If **System** already shows **Local package installed**, use the [package upgrade guide](local-pi-release.md) to keep your current build available for rollback.

### Install the upstream v0.3.12 release

Use this for the original player. The downloads, Player tab and other features listed under **Unreleased** belong to the newer fork code. This command saves the pinned installer in the current directory and starts it when the download succeeds.

```bash
curl -fsSL https://raw.githubusercontent.com/chrismtopher/chapter/v0.3.12/scripts/install-raspberry-pi.sh -o chapter-install-v0.3.12.sh &&
  bash chapter-install-v0.3.12.sh --no-reboot
```

### Install a published revision of the fork

Clone the fork on the Pi. If you already have a fresh clone from this public HTTPS URL, open it and continue with the next block.

```bash
sudo apt-get update &&
  sudo apt-get install -y git &&
  git clone https://github.com/xFrieDSpuDx/chapter.git chapter-source &&
  cd chapter-source
```

The default clone contains the fork’s published main branch. To install a different published tag or commit, check it out in this clone first. Confirm the origin and commit shown below are the ones you intend to install.

```bash
git remote get-url origin
git log -1 --oneline
```

Run the installer from that same revision and pass it the repository and full commit ID. It fetches that commit from the origin. Work present only on a developer’s computer reaches a compatible packaged Pi through a [reviewed local package](local-pi-release.md).

```bash
bash scripts/install-raspberry-pi.sh --no-reboot \
  --repo-url "$(git remote get-url origin)" \
  --release-ref "$(git rev-parse HEAD)"
```

### Boot and check the player

The installer configures the OLED, I²S amplifier, Bluetooth audio, Wi-Fi setup hotspot and web services. The installer in this checkout puts the managed application under `/opt/chapter-player/git`. The upstream v0.3.12 installer uses `/home/chapter/audiobookshelf-player` with its default service account.

If you still need to wire the hardware, shut the Pi down and follow the [wiring guide](#wire-the-hardware) before applying power again.

```bash
sudo shutdown -h now
```

If the hardware is already wired, reboot after the installer reports success.

```bash
sudo reboot
```

After reboot, reconnect over SSH and check both services. Each should report `active`. Open `http://chapter-player.local` using your Pi’s hostname, then follow [Connect Audiobookshelf](#connect-audiobookshelf). If the page or display fails to start, see [Troubleshooting](#troubleshooting).

```bash
systemctl is-active audiobookshelf-player-oled.service audiobookshelf-player-setup.service
```

## Wire The Hardware

Power the Pi off before wiring. Disconnect power, then wire the parts.

The [illustrated v5 build guide](visual-build-guide.md) shows where each board fits in the case. Use the [complete wiring diagram](assets/chapter-v5-wiring.svg) to trace all the leads together, then [test the power harness](visual-build-guide.md#test-the-power-harness) before connecting it to the Pi.

All `GPIO` numbers below use Raspberry Pi BCM numbering. The separate `Physical Pin` column identifies the numbered position on the 40-pin header. Do not confuse the two numbering systems.

### OLED

| OLED Pin | Raspberry Pi Signal | Physical Pin |
| --- | --- | ---: |
| `VCC` | 3.3 V | 1 |
| `GND` | GND | 6 |
| `SCL` / `CLK` | GPIO11 / SCLK | 23 |
| `SDA` / `DIN` | GPIO10 / MOSI | 19 |
| `CS` | GPIO8 / CE0 | 24 |
| `DC` | GPIO24 | 18 |
| `RES` / `RST` | GPIO25 | 22 |

### Select Knob (KY-040)

Power the KY-040 from 3.3 V, not 5 V.

| KY-040 Pin | Raspberry Pi Signal | Physical Pin |
| --- | --- | ---: |
| `+` | 3.3 V | 1 |
| `GND` | GND | 9 |
| `CLK` | GPIO5 | 29 |
| `DT` | GPIO6 | 31 |
| `SW` | GPIO13 | 33 |

### Volume Knob (KY-040)

| KY-040 Pin | Raspberry Pi Signal | Physical Pin |
| --- | --- | ---: |
| `+` | 3.3 V | 17 |
| `GND` | GND | 14 |
| `CLK` | GPIO12 | 32 |
| `DT` | GPIO16 | 36 |
| `SW` | GPIO26 | 37 |

### MAX98357A I2S Amp

| Amp Pin | Raspberry Pi Signal | Physical Pin |
| --- | --- | ---: |
| `VIN` | 5 V | 2 or 4 |
| `GND` | GND | 20, 25, 30, 34, or 39 |
| `BCLK` | GPIO18 | 12 |
| `LRC` / `LRCLK` | GPIO19 | 35 |
| `DIN` | GPIO21 | 40 |
| `SD` / `SD_MODE` | Leave disconnected | - |
| Speaker `+` / `-` | Speaker terminals | - |

The default software leaves `SD` / `SD_MODE` alone. That lets the breakout's own mode resistor choose the default mono mix.

The MAX98357A speaker output is differential. Connect the speaker only between the amplifier's `+` and `-` speaker terminals; do not connect either speaker terminal to Raspberry Pi ground. Keep the speaker pair short and, if practical, twisted together and routed away from the OLED and encoder wiring.

### USB-C Power Port And Fuse

If you are using the Adafruit 5993 vertical USB-C breakout as the rear power port, put a 1.5 A inline fuse on the positive 5 V leg coming from the USB-C board before it reaches the Raspberry Pi.

On the Adafruit 5993, solder the positive power lead to either pad labeled `VBUS` and the negative power lead to either pad labeled `GND`. The two breakout rows are duplicates, so either matching `VBUS`/`GND` pair may be used. `VBUS` is the raw 5 V supply from the USB-C connector; do not substitute a `CC`, `SBU`, `D+`, or `D-` pad for either power connection. Using red wire for `VBUS` and black wire for `GND` is recommended.

The build uses the holder and fuse assortment in [Amazon ASIN B0813Q4S6P](https://www.amazon.com/dp/B0813Q4S6P). Install the included **1.5 A fast-blow 5x20 mm fuse** in the holder; do not select one of the higher-current fuses from the assortment.

```text
Adafruit 5993 VBUS pad (+5 V) -> 1.5 A fuse -> Raspberry Pi 5 V pin 2 or 4
Adafruit 5993 GND pad  (-)    -> Raspberry Pi GND
```

Any 5 V branch for the amp should be taken from the fused 5 V side. Do not put the fuse in series with `GND`, `D+`, or `D-`.

For a lever-connector option, the [power splice guide](visual-build-guide.md#make-the-two-splices-with-lever-connectors) shows which four wires go into the fused +5 V connector and which four go into the separate ground connector.

Leave `D+`, `D-`, `CC`, and `SBU` unwired in the Chapter build. The Adafruit 5993 is used for 5 V power only.

Adafruit documents this breakout's USB-C current arrangement for 5 V at up to 1.5 A, while Raspberry Pi recommends a 2 A-capable supply for a Zero 2 W by itself. The tested Chapter build normally remains within the breakout's limit, but the 5993 is the power-path constraint. If the Pi reports undervoltage, reboots at high volume, or behaves unreliably, stop using it until you replace the input path with a regulated 5 V solution rated for at least 2 A.

Feeding the Pi through a 5 V header pin bypasses the Pi's normal power-input protection. Verify polarity and voltage before connecting it. Never power Chapter through the Adafruit breakout and the Pi's original `PWR IN` micro-USB port at the same time.

### Power Indicator LED

The small hole at the lower-right of the front cover, viewed from the outside, is for the power indicator. Insert the 3 mm LED into the hole from the back of the cover, then secure it with a small dab of hot glue. Keep glue away from the LED leads and wire joints.

Splice the LED into the USB-C power wiring as a parallel branch after the fuse. It must not be placed in series with either power lead; the Pi must still have a direct fused 5 V and ground connection.

The [Dioramo 13240](https://dioramo.com/products/13240) is rated for 5-6 V and has its current-limiting resistor already wired in. Its white-marked wire is the positive anode and its black wire is the negative cathode, so no additional resistor is required on Chapter's 5 V rail.

```text
Fused USB-C 5 V ----+------------------------> Raspberry Pi 5 V / amp 5 V
                    |
                    +-----------------------> Dioramo 13240 white-marked wire (+)

USB-C GND ----------+------------------------> Raspberry Pi GND / amp GND
                    |
                    +-----------------------> Dioramo 13240 black wire (-)
```

The LED illuminates as soon as USB power is present, providing immediate feedback while the Pi boots and before the OLED service starts.

## First Boot

After the installer has run and the hardware is wired, power the Pi on.

On first boot the OLED can show `chapter` followed by one of these:

- setup page address
- setup hotspot instructions
- library home screen

The installer starts the setup page and OLED services automatically on boot.

## Open The Setup Page

If the Pi is on your Wi-Fi, open one of these from a phone or computer on the same network:

```text
http://chapter-player.local
http://<the-pi-ip-address>
```

On the fork build described here, **Overview** shows the current book, Night mode and download activity. Use **Connections** for Wi-Fi and Audiobookshelf sign-in, and **Settings** for the OLED screen saver, spoken navigation and other listening preferences. Upstream v0.3.12 labels its connection tab **Setup**.

Browse the Library by title, series, author, collection, playlist, pinned shelf, listening progress or letter. Chapter uses Audiobookshelf artwork for library cards and the current book. Choose **Downloaded only** in Library to show local copies, or open **Downloads** for transfers, completed titles and free space.

The setup page also runs directly on port `47831`:

```text
http://chapter-player.local:47831
```

On the local build, the web interface is open until you set a Chapter owner login in **Settings → Security**. Once configured, visitors see only the read-only pages you allow. Use it on a trusted private home network. Do not expose ports `80` or `47831` to the internet or configure router port forwarding for the player. See the [security policy](../SECURITY.md) for the complete network-safety guidance.

If the Pi is not connected to Wi-Fi, the setup service should start an open setup hotspot:

```text
Network:  Chapter-Setup
Password: none
Page:     http://10.42.0.1
```

Connect a phone or laptop to `Chapter-Setup`, then open `http://10.42.0.1`.

## Connect Audiobookshelf

On the fork build, open **Connections → Audiobookshelf**. On upstream v0.3.12, open **Setup → Audiobookshelf**. Enter the following details.

- Audiobookshelf server URL, for example `https://books.example.com`.
- Username for the dedicated player user.
- Password for that user.

The password is sent to Audiobookshelf once. The player stores the returned user token, not the password.

Choose **Log in to Audiobookshelf** (upstream v0.3.12 uses **Log In Player**). On the fork build, select a particular library or reload its contents under **Settings → Books → Audiobookshelf library**. Choose **Use library** or **Refresh library**.

## Using The Player

- Turn the Select knob to browse books.
- Click the Select knob to choose.
- Turn the Volume knob to change volume.
- Click the Volume knob to mute or unmute.
- Hold the Volume knob for 10 seconds to show the setup address.
- On the standard software volume control, the knob moves in one-point steps below 10%. Spoken navigation announces mute and volume changes at 10% intervals.
- Hold Select for three seconds to open Bluetooth. From either Bluetooth screen, hold Select again to return to Library.

The [listening guide](listening-guide.md) walks through browsing, the Player tab, Bluetooth and listening at night. For trips away from Wi-Fi, follow the [offline listening guide](downloads-and-offline-listening.md).

The [two-knob quick reference](first-boot-one-sheet.md#control-the-same-book-from-either-place) shows how to cancel, pause and stop from Chapter or the web player.

### Audio output and Night mode

Open **Connections → Check output and test sound** to see the route selected for the open book. The built-in speaker and Bluetooth buttons play a quiet chime without changing the playback route. Unmute first. Test Bluetooth before opening a book so the output is free.

In **Settings → Listening and Night mode**, choose **On** to apply the configured OLED brightness and volume limit immediately, **Off** to use normal brightness and volume, or **Automatic** to follow the saved hours.

### Back Up And Restore Settings

Open **System → Download settings backup** to save a JSON file with your preferences, including audio feedback levels, screen settings, library order, Night mode and the pinned shelf. On the target player, open **System → Restore settings**, choose the file and preview the changes before applying them. Sign in to the same Audiobookshelf library to restore its pinned shelf.

A settings backup carries preferences. On the same Pi, restoring it keeps your downloaded books, download queue and saved listening positions. When moving to another Pi, let Chapter sync your progress to Audiobookshelf first, then sign in to the same account and download the books you want there.

## Owner access and password recovery

Open **Settings → Security** to set the owner username and password. Once you enable login, use **Access without login** to choose whether visitors can see the read-only Overview or the read-only Library and Downloads. These are private by default. Settings, connection management, device commands, playback controls and download actions require the owner account.

Existing password-only installs use `admin` as the username. On the verified local package, connect over SSH with your Pi login and run the following commands. The first command selects the root-owned copy that is actually running.

```bash
cd /opt/chapter-player/current &&
  sudo /usr/bin/python3 -EsB -m abs_kids_player.owner_auth set-password
```

If you use a release checkout that supports owner login, run the same command from its root-owned install directory. Log in with the owner username and new password. Add `--username YOUR_USERNAME` to the command if you also need to reset the username. Changing the login details signs out existing browser sessions.

## Updating Later

### Installed from a local Debian package

If **System** says **Local package installed**, follow the [local Pi release guide](local-pi-release.md) to install the matching package, verify it after a reboot or return to the bundled previous build. Both builds use the same downloads and listening state.

### Installed from a published release

To reinstall upstream v0.3.12, use the [pinned upstream installer](#install-the-upstream-v0312-release). For a newer release, use that release’s instructions. On a managed Git installation made with this fork’s revised installer, **System** checks the repository selected at installation. The in-app updater requires a newer compatible stable release tag in that repository.

Review the offered version and select **Install Update**. Chapter pauses playback, preserves device configuration and listening state, installs the confirmed release, and restarts the player services. It checks that both services stay up and that the setup service reports a fresh OLED frame. If verification fails, Chapter restores the previous release and its deployment files and reports the result in **System**. If the Pi restarts mid-update, the installed recovery service restores the previous deployment before Setup and OLED start. Keep the player powered on and connected to the internet until the administration page becomes available again.

## Troubleshooting

### SSH Does Not Connect

Check that SSH was enabled in Imager and that the Pi joined a 2.4 GHz Wi-Fi network. Use your Imager username and hostname. If `.local` does not work, use the IP address from your router.

### OLED Is Blank

On a configured player, press or turn a knob once. Check **Settings → Display → OLED burn-in protection** for an idle timeout or scheduled hours. If the screen stays dark, open **System → System status and diagnostics** to check the active build. On the Pi, check for the SPI device and the service account’s group memberships:

```bash
ls /dev/spidev*
groups chapter
```

Look for OLED startup or GPIO errors in the [service logs](#view-service-logs). If `/dev/spidev0.0` is missing, check that SPI is enabled. If the display went blank immediately after a local package upgrade, use the [packaged rollback](local-pi-release.md#roll-back). Power off before checking the OLED power and signal leads.

### No Sound

Unmute Chapter, check the volume and open **Connections → Check output and test sound**. Stop the book before testing Bluetooth. If the internal speaker is still silent, check that ALSA sees the amp on the Pi:

```bash
aplay -l
speaker-test -t wav -c 2
```

Press `Ctrl+C` to stop the speaker test.

If the card is missing, check the I²S audio overlay and reboot after correcting it. If there is a sound card but no audio, re-check the speaker wires and MAX98357A `VIN` and `GND`.

### Setup Page Does Not Open

Check the setup service and the HTTP response on the Pi. A redirect to `/owner` means the site is reachable and you can log in from a browser.

```bash
systemctl status audiobookshelf-player-setup.service
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:47831/health
```

If the direct port works but `http://chapter-player.local` does not, check the port 80 proxy:

```bash
systemctl status audiobookshelf-player-port80-forward.service
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost/health
```

### The page opens but a setting will not save

Reopen the setting to check its saved value. If Chapter reports that storage is full, open **Downloads** to check the available space. You can remove a local copy you have finished with, then return to the setting and save again. Removing a local copy keeps the original in Audiobookshelf.

When the web player’s volume or playback status stops updating, try the knobs. Chapter retries publishing its current status after storage becomes writable again. For a recurring error, note which page or control you used and check the service logs below.

### Podcasts shows “Podcasts unavailable”

From an empty Podcasts list, click **Back**, then choose **Podcasts** again. If a retry also fails, check Wi-Fi and the podcast source. You can return to the library and choose a downloaded book while the source is unavailable.

### The Pi Shows The Setup Hotspot Instead Of Joining Wi-Fi

Connect to `Chapter-Setup`, open `http://10.42.0.1`, and enter the home Wi-Fi credentials. The setup page uses NetworkManager to switch from the hotspot to the selected Wi-Fi network.

### View Service Logs

```bash
sudo journalctl -b -u audiobookshelf-player-setup.service -n 80 --no-pager -o cat
sudo journalctl -b -u audiobookshelf-player-oled.service -n 80 --no-pager -o cat
```

### Measure book startup

For an overview, use **System → System status and diagnostics → Recent playback starts**. For the detailed timing line on the permanent local package, run the command below on the Pi, then choose **Start**, **Start over** or **Continue** with the volume up.

```bash
sudo journalctl -b -u audiobookshelf-player-oled.service -n 0 -f -o cat --grep='^Playback timing '
```

On an older installation with OLED output redirected to a file, `systemctl show audiobookshelf-player-oled.service -p StandardOutput` identifies that destination. Each timing line reports milliseconds for one request that opens a new playback session.

| Field | What it measures |
| --- | --- |
| `input_ms` | Time from the confirmation knob release to the player accepting the request. |
| `audiobookshelf_ms` | The Audiobookshelf session call, including progress lookup when continuing, the play request and local response processing. Podcasts have a separate `podcast_ms` field. |
| `prepare_ms` and `owner_wait_ms` | Total session preparation, including the source time above, followed by the wait for the playback thread. |
| `gst_pre_preroll_ms` | Time from entering the playback engine to requesting the PAUSED state. The setup fields below break down this total. |
| `gst_preroll_ms` and `gst_seek_ms` | The GStreamer wait for initial media and the wait after seeking to the starting position. Streaming or transcoding delays can appear here. |
| `gst_preroll_wait` and `gst_seek_wait` | The result of each state wait. `completed` means GStreamer finished the state change and `timeout` means it was still pending after five seconds. `seek_started` means GStreamer accepted a resume seek during initial preroll. `no_preroll`, `failed`, `error` and `unavailable` identify other outcomes. A successful **Start** or **Start over** at 1× from the beginning shows `gst_seek_wait=skipped` and omits `gst_seek_ms`. |
| `gst_seek_mode` and `gst_early_seek_attempt_ms` | `early` means the resume seek was accepted while initial preroll was pending. `after_preroll` means the seek followed the initial wait. The attempt offset is measured from the start of preroll and also appears when Chapter tried an early seek and fell back to the later one. |
| `gst_pause_call_ms` and `gst_preroll_wait_ms` | Time spent requesting the PAUSED state and then waiting for that state change to finish. Together they make up `gst_preroll_ms`, subject to rounding. |
| `gst_preroll_to_seek_ms` and `gst_seek_to_play_ms` | Time between the preroll wait and the seek, then between the seek wait and the PLAYING request. The first interval includes detaching the preroll probes. These fields appear when Chapter seeks. |
| `gst_ms` | Total startup work on the playback thread through the end of the existing volume fade. |
| `gst_to_output_ms` and `select_to_output_ms` | Time from GStreamer startup and from the confirmation click to the first observed increase in the audio sink’s rendered-buffer count after the gain rises. These appear with `status=output_observed`. |

Each setup field measures the duration of one step before preroll.

| Field | Setup step |
| --- | --- |
| `gst_stop_previous_ms` | Stop and release the previous player, if one exists. |
| `gst_bindings_ms` and `gst_init_ms` | Import the Python GStreamer bindings and run `Gst.init()`, respectively. |
| `gst_playbin_create_ms` and `gst_tempo_filter_ms` | Create the player and set up its tempo filter. |
| `gst_session_setup_ms` | Set the session, select the initial track and set the starting gain. |
| `gst_media_url_ms` | Prepare the authenticated Audiobookshelf media URL, including a token refresh if needed. |
| `gst_reset_to_null_ms`, `gst_audio_sink_setup_ms` and `gst_set_uri_ms` | Reset the new player to NULL, configure its output and assign the media URI. |
| `gst_preroll_probe_setup_ms` | Attach the preroll probes. |
| `gst_pre_preroll_other_ms` | The remainder of the setup interval after subtracting the measured steps. |

The following checkpoints are offsets from the start of preroll. Compare earlier and later offsets to see where time passes. Each interval can include waiting for data, buffering and GStreamer setup.

| Field | Checkpoint |
| --- | --- |
| `gst_source_setup_ms` | Playbin announced the media source. |
| `gst_first_source_buffer_ms` | The source delivered its first data buffer. This can include time spent opening the stream, waiting on the server or reading the medium. |
| `gst_format_detected_ms` | A GStreamer typefinder announced the media type. |
| `gst_first_demux_input_ms` and `gst_first_demux_output_ms` | First data into a demuxer and the first buffer leaving any demuxer output pad. |
| `gst_first_demux_audio_ms` | First buffer leaving a demuxer pad identified as audio by its negotiated caps, stream type or audio pad name. |
| `gst_first_parser_input_ms` and `gst_first_parser_output_ms` | First data into and out of an audio parser, when the stream uses one. |
| `gst_first_decoder_input_ms` and `gst_first_decoder_output_ms` | First data into and out of an audio decoder. |
| `gst_first_decoded_audio_ms` | The first decoded audio reached the input of Chapter’s tempo filter. |
| `gst_audio_sink_added_ms` and `gst_first_sink_buffer_ms` | Playbin added a concrete audio sink and delivered the first buffer to a sink. Sink setup can overlap the other checkpoints. |

`gst_source_to_decoded_audio_ms` is the elapsed time from the first source buffer to the first decoded buffer at Chapter’s tempo filter. `gst_after_first_sink_buffer_ms` measures the remaining wait after the first buffer reaches a sink.

Fields appear for checkpoints observed during preroll. Some streams skip stages, and a timeout can end the measurement before a buffer arrives. If several elements of the same kind are present, each checkpoint records the first observation across those elements. [GStreamer playbin](https://gstreamer.freedesktop.org/documentation/playback/playbin.html) · [GStreamer pad probes](https://gstreamer.freedesktop.org/documentation/additional/design/probes.html)

`gst_demuxers` lists the demuxer plugin factories seen in preroll. `gst_demux_input_factory`, `gst_demux_output_factory` and `gst_demux_audio_factory` identify the factory associated with each timestamp. An output timestamp without an audio timestamp means a buffer was observed and no output pad had been identified as audio at the time of a buffer.

The sink timing is a software estimate. Speaker or Bluetooth buffering and silence at the start of the recording can affect when you hear sound. If the sink counter is unavailable, `select_to_gain_ms` still measures click to the start of the volume fade. Compare runs at the same position, speed and audio output, and record whether Audiobookshelf reports direct play or transcoding.

#### Compare Chapter's Continue request with the browser

Set `CHAPTER_TRACE_HTTP_RANGES=1` in the OLED service environment and restart it. Watch for `Playback timing` and `Playback HTTP range` lines while opening an Audiobookshelf session.

- `book_resume_s` is the saved position in the whole book. `track_number` is the selected track's 1-based position in the session, `track_start_s` is its offset in the book and `track_seek_s` is the position within that track.
- `track_file_id` is the numeric Audiobookshelf file ID when available. It matches the `ino` value or the ID at the end of the `/file/…` path. Compare `track_size_bytes` with the track's `metadata.size` or the total at the end of `Content-Range`. `abs_play_method` is the server's `playMethod`.
- Each `Playback HTTP range` line shows a response as reported by the GStreamer source through initial play, including the resume seek. `request_range` comes from the request's `Range` header. `content_range` gives the response's range and total size with the `bytes` unit omitted. The trace omits absent headers and omits credentials, URLs and file paths. `gst_http_responses=0` means the source exposed no usable HTTP metadata during capture.

Use the same Audiobookshelf account and run one player at a time. Close the web player, open the browser's **Network** tools and enable **Disable cache**. Continue the book in the browser. On the fresh `/play` response, compare `currentTime` with Chapter's `book_resume_s` and check `playMethod`. If progress advanced between runs, restore the bookmark in the web player, let it sync and capture again. Compare the first media requests for the matching file ID in order. Record **Status**, request **Range** and response **Content-Range** from each request's **Headers** tab. [Audiobookshelf API](https://api.audiobookshelf.org/#play-a-library-item-or-podcast-episode) · [Chrome Network view](https://developer.chrome.com/docs/devtools/network/reference/#headers) · [GStreamer HTTP source](https://github.com/GStreamer/gstreamer/blob/1.26.2/subprojects/gst-plugins-good/ext/soup/gstsouphttpsrc.c#L1415-L1433)

#### Trace source reads during qtdemux preroll

Set `CHAPTER_TRACE_SOURCE_READS=1` in the OLED service environment and restart it. Watch for `Playback timing`, `Playback source reads` and `Playback source read` lines. The source trace appears when Chapter can probe the source pad and sees `qtdemux` during preroll.

- The summary spans the first input to `qtdemux` through its first audio output or the end of preroll. If Chapter observes the element but misses its first input, the window starts when the element is added. `buffers` and `bytes` count the data observed within this window.
- `max_gap_ms` includes the intervals with no observed source buffer at the beginning and end of the window. `tail_gap_ms` covers the wait after the last buffer. These are gaps in data delivery at the source pad and can include network waits, downstream backpressure or scheduling.
- Detail lines show a bounded sample of buffer arrivals, including the first and last arrivals, the longest gaps and some offset jumps. `bytes_range` gives inclusive byte offsets when the source supplies them. Byte seeks and HTTP range responses can appear before the summary window, and are timestamped as observed at the source pad.
- The trace records HTTP status and numeric byte ranges from source events. URLs, media payload and other headers stay out of the trace. If a sample limit is reached, the corresponding truncation flag is set. Counts then cover retained samples and the summary omits gap estimates if buffer capture was truncated.

All `at_ms`, `start_ms` and `end_ms` values are offsets from the beginning of preroll. [GStreamer pad probes](https://gstreamer.freedesktop.org/documentation/additional/design/probes.html) · [GStreamer buffer offsets](https://gstreamer.freedesktop.org/documentation/gstreamer/gstbuffer.html) · [HTTP source implementation](https://github.com/GStreamer/gstreamer/blob/1.26.2/subprojects/gst-plugins-good/ext/soup/gstsouphttpsrc.c)

#### Trace slow GStreamer initialization

For a temporary trace, set these variables in the OLED service environment and restart the service.

```ini
[Service]
Environment=CHAPTER_TRACE_GST_INIT=1
Environment=GST_DEBUG=GST_INIT:4,GST_REGISTRY:5,GST_PLUGIN_LOADING:5
Environment=GST_DEBUG_NO_COLOR=1
```

Chapter logs `GStreamer init phase=begin` and `phase=end` around the call. The native trace goes to standard error and timestamps registry cache reads, registry updates and plugin loader activity. Use the process ID on Chapter’s markers to identify the main process. Keep the existing registry cache for the retest so it follows the normal startup path. Debug output can add latency. [GStreamer debugging options](https://gstreamer.freedesktop.org/documentation/gstreamer/running.html)

## References

- Raspberry Pi getting started guide: https://www.raspberrypi.com/documentation/computers/getting-started.html
- Raspberry Pi Zero 2 W hardware information: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html
- Raspberry Pi configuration guide: https://www.raspberrypi.com/documentation/computers/configuration.html
- Adafruit MAX98357A Raspberry Pi setup: https://learn.adafruit.com/adafruit-max98357-i2s-class-d-mono-amp/raspberry-pi-usage
