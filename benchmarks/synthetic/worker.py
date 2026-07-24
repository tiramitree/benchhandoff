"""Synthetic child: leave a partial file once, then complete on resume."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    attempt = int(os.environ["BENCHHANDOFF_ATTEMPT"])
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    payload = source.read_bytes()
    if attempt == 1:
        destination.write_text("partial-result\n", encoding="utf-8")
        os._exit(75)

    result = {
        "attempt": attempt,
        "input_sha256": hashlib.sha256(payload).hexdigest(),
        "input_size": len(payload),
    }
    destination.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
