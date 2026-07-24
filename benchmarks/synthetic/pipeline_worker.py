"""One deterministic stage for the 12-task restart-versus-resume comparison."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    task_number = int(sys.argv[1])
    source = Path(sys.argv[2])
    destination = Path(sys.argv[3])

    if os.environ.get("SYNTHETIC_MODE") == "naive":
        should_fail = task_number == 6 and os.environ.get("SYNTHETIC_PASS") == "1"
    else:
        should_fail = task_number == 6 and os.environ.get("BENCHHANDOFF_ATTEMPT") == "1"

    payload = source.read_bytes()
    if should_fail:
        destination.write_bytes(payload + b"partial-task-06\n")
        return 75

    destination.write_bytes(payload + f"task-{task_number:02d}\n".encode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
