"""Fail once, then create one deterministic synthetic output."""

from __future__ import annotations

import os
import time
from pathlib import Path


attempt = int(os.environ["BENCHHANDOFF_ATTEMPT"])
output = Path("result.txt")

if attempt == 1:
    time.sleep(45)
    output.write_text("partial\n", encoding="utf-8")
    raise SystemExit(17)

source = Path("input.txt").read_text(encoding="utf-8")
output.write_text(source.upper(), encoding="utf-8")
