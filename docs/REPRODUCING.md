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

Use the single package entrypoint from a clean exact checkout. It refuses a
dirty source tree, an existing output path, and any output inside the checkout.
On Linux:

```bash
evidence_parent="$(mktemp -d)"
PYTHONPATH=src python benchmarks/synthetic/reproduce.py \
  --output-dir "$evidence_parent/package"
```

On PowerShell:

```powershell
$evidenceParent = New-Item -ItemType Directory -Path (
  Join-Path ([IO.Path]::GetTempPath()) ("benchhandoff-" + [guid]::NewGuid())
)
$env:PYTHONPATH = "src"
python benchmarks\synthetic\reproduce.py `
  --output-dir (Join-Path $evidenceParent.FullName "package")
```

The new package contains:

- `focused-recovery.json`;
- `pipeline-comparison.json`;
- `summary.json`, which binds the source commit, bounded environment, record
  hashes, and asserted synthetic claims; and
- `SHA256SUMS.txt`, which hashes all three JSON records; and
- `PACKAGE_COMPLETE.json`, written last, which binds the manifest identity and
  required file list.

The entrypoint requires a newly created, empty, caller-private parent directory;
it rejects symlink or reparse-point parents and rechecks the parent identity
before writing. This narrows accidental path races but is not a defense against
a privileged concurrent attacker. It validates the exact assertions and
re-hashes every record after writing. `PACKAGE_COMPLETE.json` is written last;
a directory without a valid completion record, exact file set, and verified
manifest is incomplete and must not be cited or submitted. The script never
writes evidence into the checkout, because doing so would make later provenance
observe a dirty source tree.

The pipeline comparison is expected to assert 18 child calls for naive full
restart versus 13 for BenchHandoff resume, with five versus zero repeated
successful tasks and the same final output hash. These are deterministic counts
for a synthetic 12-task fixture. They are not elapsed-time, cost, GPU,
real-workload, production, or third-party performance measurements.

## Share a bounded reproduction report

A useful report contains:

- the exact commit or released version;
- operating system and Python implementation/version;
- the exact reproduction command;
- test pass/fail/error/skip counts;
- the complete `SHA256SUMS.txt` values for the bounded JSON records;
- deviations from this guide; and
- a link to immutable evidence when one exists.

Do not share raw run directories, task logs, absolute paths, command-line
secrets, environment dumps, credentials, or private inputs. Reproduce a problem
with new synthetic inputs and share only the bounded report fields listed above.

Use the structured reproduction-report issue after the repository is public.
Submitting a report documents one attempt; it does not by itself prove
production use, broad compatibility, security, scientific validity, or ongoing
adoption.
