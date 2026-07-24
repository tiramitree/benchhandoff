"""Create deterministic synthetic features for the recovery example."""

from __future__ import annotations

import json
import sys
from pathlib import Path


source = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
features = {"features": [value * 2 for value in source["samples"]]}
Path(sys.argv[2]).write_text(
    json.dumps(features, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
