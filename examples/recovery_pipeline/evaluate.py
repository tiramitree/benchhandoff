"""Fail once after a partial write, then produce deterministic synthetic metrics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


features = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["features"]
scale = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))["scale"]
output = Path(sys.argv[3])
attempt = int(os.environ["BENCHHANDOFF_ATTEMPT"])

if attempt == 1:
    output.write_text('{"status":"partial"}\n', encoding="utf-8")
    raise SystemExit(17)

metrics = {
    "count": len(features),
    "scaled_sum": sum(features) * scale,
    "status": "synthetic-complete",
}
output.write_text(
    json.dumps(metrics, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
