# Contributing to Chapter

Describe the behaviour a listener sees, why the change helps and how to verify it. Keep fixes, file moves and mechanical formatting in reviewable changes.

## Where to start

| Area | Code |
| --- | --- |
| Turning or pressing the knobs | `oled_input.py` reads the controls. `rotary_ui.py` owns navigation and produces menu commands. `oled_service.py` connects them to the running player. |
| Opening bookmarks on the OLED | `oled_bookmarks.py` owns the pending lookup for one opening of the picker. It applies a result only while the same session, account and picker are active. |
| Refreshing the library on the OLED | `oled_catalogue.py` schedules Audiobookshelf lookups. `oled_shelf.py` checks completed downloads and saved positions in the background. The OLED applies a result to the menu only while its source catalogue, account, sort order and displayed list still match. |
| Checking device status | `device_status.py` returns the last sample while a worker checks the device. The OLED uses it for Wi-Fi. The dashboard uses it for Wi-Fi and Bluetooth. |
| Starting, cancelling or stopping a book | `playback_owner.py` serialises player commands. `playback_start.py` prepares a book and `playback.py` operates the playback engine. |
| Listening progress | `progress_sync.py` records local edits and reconciles them with Audiobookshelf. |
| Offline books | `download_queue_store.py` persists requests, `download_transfer.py` transfers audio and `download_store.py` checks which tracks can be played. |
| Downloads in the browser | `setup_server.py` checks access before dispatch. `download_routes.py` handles download actions, preferences and polling. `download_views.py` renders the cards and queue. |
| Web requests and preferences | `setup_server.py` checks access and routes requests. `setup_http.py` reads the complete form and owns HTTP responses. `device_preferences.py` applies individual controls. Both services use `config.update_config` to change settings. |
| Live updates in the browser | `web_player_events.py` builds the player status and streams changes. The request handler supplies the current access check. |
| Pi installations | `scripts/install-raspberry-pi.sh` installs published releases. `scripts/local_release/` packages and promotes a working tree on an existing appliance. |

## Coding standards

- Give a responsibility one owner. Use descriptive names, type hints at module boundaries and straightforward control flow. Split an area when its callers or reasons for change differ.
- Keep hardware and network operations at their interfaces. Run network calls outside configuration transactions. Use real files when testing persistence and separate processes when testing file locking.
- Preserve previously saved settings, listening positions, credentials and downloads. Exercise the normal knob, web or service entry point as well as failure and retry.
- When a background check serves an interactive screen, hold the check open in a test and use a control before releasing it. For a settings form, test an interrupted submission against a real temporary settings file, then retry the complete form.
- Justify new runtime dependencies on the Pi Zero 2 W. Keep development tools in the development environment.
- Use Ruff for linting. Formatting covers the modules and tests listed in the check script. Keep layout-only changes separate from behaviour fixes.

## Run the checks

Use Python 3.11 or later on macOS or Linux. From the repository root run

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
bash scripts/check.sh
```

CI also runs the browser tests with Playwright, Chromium and axe-core. The workflow gives the pinned versions and browser command. The accessibility journey signs in to a temporary local Chapter server, uses keyboard controls to open and stop a book, and checks focus, accessible names and download announcements. Its simulated device consumes real web commands. Include Pi evidence for changes to display, sound and controls. For installation changes, identify the running build and verify the downloaded library across a full reboot and rollback.

The separate native playback-speed job runs on Ubuntu 24.04 using its system Python, `python3-gst-1.0`, and the base and good GStreamer plugins. It uses a local WAV file and a fake audio sink. To repeat it on an Ubuntu machine with those packages installed, run the following from the repository root. The required mode makes a missing binding or plugin fail the job.

```sh
CHAPTER_REQUIRE_GSTREAMER=1 PYTHONPATH=tests /usr/bin/python3 -B -m unittest -v \
  test_gstreamer_speed.NativeSpeedTest.test_rate_changes_and_resume_reach_the_audio_filter
```

### Measure input and dashboard latency

From a development checkout, run `python scripts/measure_responsiveness.py`. The benchmark creates a temporary library with 1,000 and then 5,000 titles, including 50 one-track downloads. It queues a volume turn at the start of a settings check and times how long the OLED service takes to issue the matching display frame. It also times complete owner Overview responses over a local socket with an immediate Wi-Fi probe and with a 250 ms probe. Each request starts with an empty Wi-Fi cache.

The default is 20 samples per case, with dial turns one second apart. The output gives the median and 95th percentile in milliseconds. GPIO debounce, panel transfer time, a real NetworkManager check and browser painting are outside these measurements.
