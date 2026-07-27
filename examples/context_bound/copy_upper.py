from __future__ import annotations

import sys
from pathlib import Path


source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
