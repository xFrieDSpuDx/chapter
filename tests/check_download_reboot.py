"""Run the two halves of the on-device retention check in the extracted test bundle."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "download_retention_trial.py"


def run_phase(home: Path, build: Path, phase: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-s", str(FIXTURE), phase], cwd=build, timeout=30,
        env={"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(build)},
        capture_output=True, text=True,
    )
    expected_exit = 73 if phase == "seed" else 0
    if result.returncode != expected_exit:
        raise RuntimeError(f"{phase} failed ({result.returncode}).\n{result.stdout}{result.stderr}")


def check_reboot(home: Path, step: str, boot_id: str) -> str:
    marker = home / ".chapter-test-home"
    finished = home / "verified"
    if step == "before":
        home.mkdir(mode=0o700, exist_ok=False)
        marker.write_text(boot_id)
        run_phase(home, ROOT, "seed")
        run_phase(home, ROOT, "offline")
        return "Ready. One complete book and one partial download are on disk. Run 'after' following a normal reboot."
    if not marker.is_file():
        raise ValueError("Run 'before' first.")
    if marker.read_text() == boot_id:
        raise ValueError("The Pi is still on the same boot. Run 'after' following a normal reboot.")
    # The queue is persistent. Re-running after an interrupted check is safe.
    if not finished.is_file():
        if not (home / "resuming").exists():
            run_phase(home, ROOT, "offline")
            (home / "resuming").touch()
        run_phase(home, ROOT, "resume")
    run_phase(home, ROOT, "verify")
    finished.touch()
    return "Passed. Both audio files match the originals after a full Pi reboot and a resumed download."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("before", "after"))
    args = parser.parse_args()
    try:
        print(check_reboot(ROOT / "test-home", args.step,
                           Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    except FileExistsError:
        parser.exit(1, "This folder already holds a test. Use 'after' or unpack a fresh test folder.\n")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
