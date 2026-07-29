"""Fail once, then create one deterministic synthetic output."""

from __future__ import annotations

import os
import time
from pathlib import Path


def wait_for_release(name: str) -> None:
    """Keep the synthetic runner live until the E2E gate releases it."""

    marker = Path("/tmp", name)
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        time.sleep(0.2)
    raise SystemExit(78)


attempt = int(os.environ["BENCHHANDOFF_ATTEMPT"])
output = Path("result.txt")

if attempt == 1:
    wait_for_release("start.release")
    output.write_text("partial\n", encoding="utf-8")
    raise SystemExit(17)

wait_for_release("resume.release")
source = Path("input.txt").read_text(encoding="utf-8")
output.write_text(source.upper(), encoding="utf-8")
