"""Measure the installed Chapter player through its normal display and HTTP paths."""

import argparse
import re
import statistics
import subprocess
import time
from getpass import getpass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener


BASE = "http://127.0.0.1"
OVERVIEW = "/?tab=overview"
OLED = "audiobookshelf-player-oled.service"
MARKER = "/run/chapter-dial-timing"


class OwnerPage(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.dashboard = False
        self.token = ""
        self.feed(body.decode("utf-8"))

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "div" and "data-dashboard" in values:
            self.dashboard = True
        if tag == "input" and values.get("name") == "csrf_token":
            self.token = values.get("value", "")


def request(opener, path, fields=None):
    body = urlencode(fields).encode() if fields is not None else None
    start = time.perf_counter()
    with opener.open(BASE + path, data=body, timeout=15) as response:
        headers_ms = (time.perf_counter() - start) * 1000
        html = response.read()
        total_ms = (time.perf_counter() - start) * 1000
    return OwnerPage(html), headers_ms, total_ms


def dashboard():
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    first = request(opener, OVERVIEW)
    sign_out_token = ""
    if not first[0].dashboard:
        owner, _, _ = request(opener, "/owner")
        if not owner.token:
            raise ValueError("Chapter did not return its owner sign-in form.")
        username = input("Chapter owner username: ")
        fields = {"csrf_token": owner.token, "username": username, "password": getpass("Chapter owner password: ")}
        page, _, _ = request(opener, "/owner/sign-in", fields)
        if not page.dashboard or not page.token:
            raise ValueError("Owner sign-in did not open the dashboard.")
        sign_out_token = page.token
        first = None  # Login includes a redirect. Time a normal dashboard GET instead.
    try:
        totals = []
        for sample in range(1, 8):
            page, headers, total = first if sample == 1 and first else request(opener, OVERVIEW)
            if not page.dashboard:
                raise ValueError("Expected the owner dashboard. The timing sample was discarded.")
            print(f"Overview {sample}/7  headers_ms={headers:.1f}  total_ms={total:.1f}", flush=True)
            totals.append(total)
            if sample < 7:
                time.sleep(1)
        print(f"Owner Overview through port 80: median={statistics.median(totals):.1f} ms; max={max(totals):.1f} ms")
    finally:
        if sign_out_token:
            request(opener, "/owner/sign-out", {"csrf_token": sign_out_token})


def dials(candidate):
    selected = Path("/opt/chapter-player/current").resolve()
    if selected != candidate:
        raise ValueError("The dial timing build is not selected. Run install before this check.")
    subprocess.run(["sudo", "-v"], check=True)
    input("Wake the OLED and open Library. Press Enter when ready to start the 90-second capture. ")
    since = time.time()
    try:
        subprocess.run(["sudo", "touch", "--", MARKER], check=True)
        input("Turn Select five single steps and Volume up and down five times, about a second apart. "
              "Press Enter to see the results. ")
    finally:
        subprocess.run(["sudo", "rm", "-f", "--", MARKER], check=True)
    logs = subprocess.run(
        ["sudo", "journalctl", "-b", "-u", OLED, "--no-pager", "-o", "cat", f"--since=@{since:.6f}",
         "--grep=^Dial timing "], text=True, stdout=subprocess.PIPE,
    )
    if logs.returncode not in (0, 1):
        logs.check_returncode()
    samples = {"nav": [], "volume": []}
    for line in logs.stdout.splitlines():
        if not line.startswith("Dial timing "):
            continue
        print(line)
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        if fields.get("turns") == "1" and fields.get("dial") in samples:
            samples[fields["dial"]].append(float(fields["to_frame_ms"]))
    for name, times in samples.items():
        if times:
            print(f"{name}: {len(times)} single turns, median={statistics.median(times):.1f} ms to the OLED write")
        else:
            print(f"{name}: no single-turn samples. Try a turn that visibly changes the awake screen.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dashboard")
    commands.add_parser("dials").add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "dashboard":
            dashboard()
        else:
            dials(args.candidate)
    except HTTPError as error:
        parser.exit(1, f"Chapter returned HTTP {error.code}. Dashboard timing did not complete.\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nCheck ended.\n")
    except (EOFError, URLError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Pi check could not complete: {error}\n")
