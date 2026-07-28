"""Create one deterministic synthetic output in the reviewed workspace."""

from pathlib import Path

source = Path("input.txt").read_text(encoding="utf-8")
Path("result.txt").write_text(source.upper(), encoding="utf-8")
