"""Summarize deterministic synthetic metrics."""

from __future__ import annotations

import json
import sys
from pathlib import Path


metrics = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
summary = {
    "message": f"processed {metrics['count']} synthetic samples",
    "scaled_sum": metrics["scaled_sum"],
}
Path(sys.argv[2]).write_text(
    json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
