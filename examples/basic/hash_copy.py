"""Write a JSON content identity for one input file."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: hash_copy.py INPUT OUTPUT", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    payload = source.read_bytes()
    record = {
        "input": source.as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
