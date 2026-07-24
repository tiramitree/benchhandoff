# Reproducing BenchHandoff Evidence

This guide separates three different claims:

1. **Source validation**: tests pass for one recorded source commit and
   environment.
2. **Synthetic result reproduction**: the fixed fixtures produce the asserted
   child-work counts and final hash relationship.
3. **Independent reproduction**: an outside person publishes enough evidence to
   inspect a reproduction attempt.

The first two can be performed by the maintainer. They do not establish the
third, production use, or external adoption.

## Record the source and environment

Use a clean checkout of an exact commit and record:

```bash
git rev-parse HEAD
git status --short
python --version
python -c "import platform,sys; print(platform.system() or "Unknown"); print(sys.implementation.name)"
```

Record whether Python came from a virtual environment, the operating system,
and every skipped check. Do not publish a local checkout path, username, token,
environment dump, or other identifying value.

## Run source validation

From the repository root:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests benchmarks examples
```

On PowerShell, set `$env:PYTHONPATH = "src"` first. Report passes, failures,
errors, and skips separately. Do not convert a skipped platform capability into
a pass.

## Reproduce the synthetic records

Create a new evidence directory; the output files must not already exist:

```bash
evidence_root="$(mktemp -d)"
PYTHONPATH=src python benchmarks/synthetic/run_pipeline_comparison.py \
  --output "$evidence_root/pipeline-comparison.json"
PYTHONPATH=src python benchmarks/synthetic/run_benchmark.py \
  --output "$evidence_root/focused-recovery.json"
sha256sum "$evidence_root"/*.json
python -c 'import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); h=sys.argv[2]; d=[json.loads((r/n).read_text(encoding="utf-8")) for n in ("pipeline-comparison.json","focused-recovery.json")]; assert all(x["source_git_clean"] is True and x["source_git_commit"] == h for x in d)' "$evidence_root" "$(git rev-parse HEAD)"
```

On PowerShell, create a unique directory under `[IO.Path]::GetTempPath()` and use `Get-FileHash -Algorithm SHA256`; do not write the first record into the checkout, because that would make the second record report a dirty source tree.

The pipeline comparison is expected to assert 18 child calls for naive full
restart versus 13 for BenchHandoff resume, with five versus zero repeated
successful tasks and the same final output hash. These are deterministic counts
for a synthetic 12-task fixture. They are not elapsed-time, cost, GPU,
real-workload, production, or third-party performance measurements.

## Share a bounded reproduction report

A useful report contains:

- the exact commit or released version;
- operating system and Python implementation/version;
- the exact commands;
- test pass/fail/error/skip counts;
- SHA-256 values for the raw synthetic JSON records;
- deviations from this guide; and
- a link to immutable evidence when one exists.

Do not share raw run directories, task logs, absolute paths, command-line
secrets, environment dumps, credentials, or private inputs. Reproduce a problem
with new synthetic inputs and share only the bounded report fields listed above.

Use the structured reproduction-report issue after the repository is public.
Submitting a report documents one attempt; it does not by itself prove
production use, broad compatibility, security, scientific validity, or ongoing
adoption.
