"""Measure a queued dial turn and an owner dashboard GET using disposable local fixtures."""

# The benchmark runs directly from an uninstalled checkout and reuses its service test fixtures.
# ruff: noqa: E402
import argparse
import contextlib
import io
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch


root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]

from test_oled_service import OledServiceTest
from test_setup_http import SetupRequestRecoveryTest

from abs_kids_player import setup_server
from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.config import AppConfig
from abs_kids_player.display_frames import VolumeFrame
from abs_kids_player.download_store import DownloadedBook, book_directory
from abs_kids_player.models import AudioTrack, Book
from abs_kids_player.oled_input import OledInputQueue
from abs_kids_player.wifi import WifiStatus


def fixture(config, size):
    books = [Book(str(i), f"Story {size - i:05}", f"Reader {i % 23}", 1200, "") for i in range(size)]
    for book in books[:50]:
        directory = book_directory(config, book.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "track-1").write_bytes(b"x")
        track = AudioTrack(1, 0, 1200, "track-1", "One", size_bytes=1)
        DownloadedBook(book, [track], [], complete=True).save(directory)
    return books


def dial_samples(size, repeats, spacing):
    config = AppConfig(server_url="https://fixture.invalid", username="reader", token="fixture", library_id="library")
    books = fixture(config, size)
    display, events = Mock(), OledInputQueue()
    times = []
    last_event = None
    started = False
    ticks = 0

    def load():
        nonlocal started, last_event
        if started:
            last_event = InputEvent("volume", 1 if len(times) % 2 else -1)
            events.put(last_event)
        started = True
        return config

    def draw(frame):
        if isinstance(frame, VolumeFrame) and last_event is not None:
            times.append((time.monotonic() - last_event.received_at) * 1000)

    def after_frame(shutdown, seconds):
        nonlocal ticks
        if seconds:
            ticks += 1
            if ticks == repeats + 1:
                shutdown.request(0, None)
            else:
                time.sleep(spacing)

    display.show.side_effect = draw
    with patch("abs_kids_player.oled_service.DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS", 0):
        OledServiceTest().run_service(display, events, config, after_frame,
                                      load_config=load, load_cached_books=lambda config: books,
                                      wifi_status=lambda: WifiStatus(False))
    if len(times) != repeats + 1:
        raise RuntimeError(f"Expected {repeats + 1} volume frames, received {len(times)}")
    return times[1:]  # Exclude the first frame, which also applies the initial network status.


def dashboard_samples(delay, repeats):
    http = SetupRequestRecoveryTest()
    http.setUp()
    times = []
    try:
        for _ in range(repeats):
            finished = threading.Event()

            def probe():
                time.sleep(delay)
                finished.set()
                return WifiStatus(True, ssid="Bench")

            with contextlib.ExitStack() as mocks:
                mocks.enter_context(patch.object(setup_server, "wifi_status", probe))
                mocks.enter_context(patch.object(setup_server.web_bluetooth, "current", return_value=(False, "")))
                mocks.enter_context(patch.object(setup_server, "download_activity_status", return_value={}))
                if hasattr(setup_server, "web_wifi"):
                    mocks.enter_context(patch.object(setup_server, "web_wifi", type(setup_server.web_wifi)()))
                start = time.perf_counter()
                status, _, _ = http.request("GET", "/?tab=overview")
                times.append((time.perf_counter() - start) * 1000)
                if status != 200 or not finished.wait(timeout=2):
                    raise RuntimeError("The benchmark did not complete a dashboard request and Wi-Fi probe")
    finally:
        http.doCleanups()
    return times


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--dial-spacing", type=float, default=1.0,
                        help="seconds between settings ticks, outside the measured input-to-frame time")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least two")
    if not math.isfinite(args.dial_spacing) or args.dial_spacing < 0:
        parser.error("--dial-spacing must be a finite non-negative number")
    results = {}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "ABS_KIDS_PLAYER_STATE_DIR": tmp, "ABS_KIDS_PLAYER_STATE_PATH": str(Path(tmp) / "player-state.json"),
        "ABS_KIDS_PLAYER_CONFIG_DIR": tmp, "ABS_KIDS_PLAYER_CONFIG_PATH": str(Path(tmp) / "config.json"),
        "ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH": str(Path(tmp) / "library-cache.json"),
    }), contextlib.redirect_stdout(io.StringIO()):
        for size in (1000, 5000):
            results[f"Dial, {size} books / 50 downloaded"] = dial_samples(size, args.samples, args.dial_spacing)
        for delay in (0, .25):
            results[f"Dashboard GET, {delay * 1000:.0f} ms Wi-Fi probe"] = dashboard_samples(delay, args.samples)
    for name, samples in results.items():
        p95 = sorted(samples)[math.ceil(.95 * len(samples)) - 1]
        print(f"{name}: n={len(samples)} median={statistics.median(samples):.2f} ms p95={p95:.2f} ms")


if __name__ == "__main__":
    main()
