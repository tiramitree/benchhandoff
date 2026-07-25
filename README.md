# BenchHandoff v0.1

BenchHandoff is a narrow local CLI for resuming a flat, sequential batch of
expensive commands. It fingerprints the suite and declared inputs, records
per-task logs and declared-output hashes, and skips a previously completed task
only when those outputs still re-verify. It turns a `suite.toml` into three
reviewable records:

- `plan.json`: the exact suite, seed-input identities, runtime facts, and task
  order accepted before execution;
- `state.json`: atomically replaced task and attempt state; and
- `bundle.json`: the final hashes for the plan, state, event chain, task logs,
  quarantined partial outputs, and verified outputs.

Before an approval-gated retry, `inspect` can also emit a deterministic resume
view and SHA-256. A caller can pass that digest back to `resume`; if any bound
evidence, relevant input, completed output, or partial output changed after
review, the runner refuses the bound resume before its first transition.

Every `start` and `resume` mutation also holds one sibling writer-lock record
created with `O_EXCL`. A second cooperating BenchHandoff process targeting the
same run is refused before it can read and then mutate run evidence. The lock
is local coordination, not a remote lease or security boundary; an orphaned
record remains fail-closed until an explicit evidence-bound recovery succeeds.

A manual point-in-time name check against PyPI, npm, crates.io, and GitHub found
no exact match on 2026-07-24. That is not a trademark or permanent-availability
conclusion; repeat the check immediately before publication. This repository
has not been published and no external adoption is claimed.

BenchHandoff is not an experiment tracker, DAG workflow engine, distributed
scheduler, sandbox, cryptographic attestation service, or guarantee of full
reproducibility.

## What v0.1 proves

For declared files, v0.1:

1. rejects absolute, parent-relative, symlinked, non-regular, Windows-aliasing,
   and ancestor-conflicting paths;
2. hashes every seed input before the first child process;
3. launches argument arrays directly with `shell=False` and preserves repeated
   argument values;
4. treats launch errors, non-zero exits, input drift, missing outputs, and
   non-regular outputs as failed runs;
5. never creates a bundle for a failed run;
6. quarantines an incomplete attempt's regular-file outputs before retry;
7. verifies the completed prefix and retry budget before a resume transition;
8. emits a mutation-free resume decision that binds current evidence, relevant
   inputs, completed outputs, and unverified output/quarantine observations;
9. optionally requires that exact decision SHA-256 and checks it again
   immediately before the first resume transition;
10. serializes cooperating local `start` and `resume` writers with an exclusive
    sibling lock and refuses contention before run-evidence mutation;
11. uses per-file same-directory atomic replacement for state and the
    content-monotonic event chain; and
12. re-hashes the complete declared evidence set and rejects unexpected run-root
    entries during `verify`.

These guarantees apply only to declared files. BenchHandoff is not a sandbox,
and its per-file atomic writes are not a cross-file transaction or a power-loss
guarantee. See [LIMITATIONS.md](LIMITATIONS.md).

## Requirements and implemented execution targets

- Python 3.11 or newer. The checked-in CI plan covers CPython 3.11 through 3.14.
- Windows or Linux for child execution. Linux requires `/proc` so the runner can
  bind a PID to a stable process-start identity.
- No third-party runtime packages.
- A suite directory and a separate run-evidence directory.
- The suite, every output's existing parent, and the run/quarantine directory
  must be on the same filesystem so quarantine moves cannot cross devices.

macOS and other operating systems are not supported for execution in v0.1; the
runner fails closed when it cannot obtain a stable child-start identity. The
optional package build uses setuptools. Running from source needs no install.

## Validation status

On 2026-07-25, the local Windows preflight exercised CPython 3.11.15, 3.12.13,
3.13.14, and 3.14.6. Each runtime completed the 122-test suite with 119 passes
and the same 3 symlink-creation permission skips, with no failures or errors.
One pre-license wheel was then installed under all four runtimes; metadata and
CLI help passed, and each installation completed the deliberate fail -> bound
resume -> verify recovery path. The checked-in CI matrix additionally covers
Ubuntu 24.04 and Windows Server 2025, but it remains a proposed plan until an
online run exists. The canonical external-evidence validator passed with all
four public counts at zero. This preflight is not a final licensed
distribution, Linux result, production result, independent reproduction, or
adoption evidence. The exact validated source is recorded in
[VALIDATION_20260724.md](VALIDATION_20260724.md).

The later cooperative writer-lock extension at clean source `d7b2cf6...` was
also exercised under CPython 3.11.15, 3.12.13, 3.13.14, and 3.14.6. Each
runtime compiled 38 Python files from source, validated the zero external-
evidence ledger, and completed the current 129-test suite with 126 passes and
the same 3 symlink-permission skips, with no failures or errors. This is four
complete runs of one suite, not one 516-test suite. The commit-bound record is
under
`benchmarks/results/windows_py311_314_writer_lock_matrix_commit_d7b2cf6_20260725/`.
It does not change the absence of Linux, public online CI, release,
independent-validation, production, or adoption evidence.

At clean source `8d789e9...`, the local CPython 3.12.10 orphan-lock diagnostic
then used two processes and a real hard exit. Two read-only recovery decisions
were identical; bound lock recovery changed zero run-evidence files, changed no
partial output, and left the attempt count at 1. A separate bound run resume
completed and verified attempt 2 while preserving the original lock tombstone.
The 2239-byte three-file record is under
`benchmarks/results/windows_py312_writer_recovery_commit_8d789e9_20260725/`.
It is local synthetic Windows control-plane evidence, not Linux validation,
safe-child-retry proof, public CI, production reliability, independent
reproduction, distributed coordination, hostile-writer protection, or
external adoption.

At clean source `9bc9233...`, the complete orphan-recovery source, test, and
diagnostic matrix then passed under CPython 3.11.15, 3.12.13, 3.13.14, and
3.14.6. Each runtime compiled 42 Python files, validated the zero external-
evidence ledger, and completed the 145-test suite with 142 passes, the same 3
Windows symlink-permission skips, and no failures or errors. Each runtime also
completed the direct two-process hard-exit recovery diagnostic with two
identical inspections, zero changed run-evidence files, attempt 1 preserved
during lock recovery, attempt 2 verified only after a separate bound resume,
and the tombstone preserved. This is four complete suite runs and four
diagnostic runs, not one 580-test suite. The 5709-byte three-file record is
under
`benchmarks/results/windows_py311_314_writer_recovery_matrix_commit_9bc9233_20260725/`.
It remains local synthetic Windows control-plane evidence, not Linux or public
CI validation, a licensed release, safe-child-retry proof, production
reliability, independent reproduction, distributed coordination, hostile-
writer protection, third-party review, or external adoption.

## Five-minute quickstart

The recovery example deliberately fails once after writing a partial output.
From the repository root in PowerShell:

```powershell
Copy-Item -LiteralPath examples\recovery_pipeline -Destination demo-recovery -Recurse
New-Item -ItemType Directory -Path runs
$env:PYTHONPATH = "src"
python -m benchhandoff start demo-recovery\suite.toml --run-dir runs\recovery
if ($LASTEXITCODE -ne 20) { throw "expected the synthetic first attempt to fail" }
$decision = python -m benchhandoff inspect runs\recovery | ConvertFrom-Json
python -m benchhandoff resume runs\recovery `
  --expected-decision-sha256 $decision.decision_sha256
python -m benchhandoff verify runs\recovery
```

The first task remains complete, the failed task's partial `metrics.json` moves
to quarantine, and only the incomplete suffix runs again. The final bundle is
written under `runs/recovery`. All copied suite outputs and the run directory
must start absent.

Equivalent commands on Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
cp -R examples/recovery_pipeline demo-recovery
mkdir -p runs
set +e
PYTHONPATH=src python -m benchhandoff start demo-recovery/suite.toml --run-dir runs/recovery
start_code=$?
set -e
test "$start_code" -eq 20
decision_sha="$(
  PYTHONPATH=src python -m benchhandoff inspect runs/recovery |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["decision_sha256"])'
)"
PYTHONPATH=src python -m benchhandoff resume runs/recovery \
  --expected-decision-sha256 "$decision_sha"
PYTHONPATH=src python -m benchhandoff verify runs/recovery
```

Activating the virtual environment matters for this example because the suite
invokes `python` by name. A success-only example remains under `examples/basic`.

## Suite format

```toml
version = 1
name = "basic-sha256"

[[task]]
id = "hash-input"
argv = ["python", "hash_copy.py", "input.txt", "output.json"]
inputs = ["hash_copy.py", "input.txt"]
outputs = ["output.json"]
```

Tasks execute in declaration order. An input may be a regular seed file that
exists when `start` runs or an output produced by an earlier task. Every output
path must be unique and absent before its task starts. Paths use portable `/`
separators and may not contain empty, `.`, `..`, `\`, `:`, or absolute
components.

The v0.1 parser bounds the suite to 256 KiB, 64 tasks, and 512 total declared
input/output references. Each task is bounded to 128 arguments, 64 inputs, and
64 outputs; additional byte and portable-path limits are enforced before the
run directory is created.

Commands receive:

- `BENCHHANDOFF_RUN_ID`
- `BENCHHANDOFF_TASK_ID`
- `BENCHHANDOFF_ATTEMPT`

Declare scripts, configs, datasets, and other material dependencies as inputs
when their identities need to be covered by evidence.

## Failure and resume

`start` exits `20` when a child fails. Its stdout and stderr remain in the run
directory, no bundle exists, and partial regular-file outputs remain unverified.
`resume`:

1. verifies the suite, seed inputs, plan, event chain, and completed prefix;
2. refuses to mutate evidence if the next-attempt budget is exhausted;
3. refuses to proceed while a recorded child may still be alive or a launch
   identity remains unresolved;
4. moves partial regular-file outputs into `quarantine/`;
5. appends a new attempt and re-runs the first incomplete task; and
6. creates a bundle only after every task output is a verified regular file.

For approval-gated recovery, run `inspect` first and pass its
`decision_sha256` to `resume --expected-decision-sha256`. The decision is a
read-only snapshot: it hashes the suite, run evidence, completed outputs,
next-task inputs, current unverified outputs, and deterministic quarantine
candidates. Bound resume recomputes it twice and exits `30` before the
`run_resumed` transition if the digest is stale. `inspect` refuses an unstable
pending event instead of reconciling it. Plain `resume` remains compatible and
may reconcile the two modeled pending-event states.

The decision is not stored, signed, or itself a lock. The mutation path holds a
sibling writer-lock record plus an automatically released operating-system
guard across both decision checks and every subsequent transition, quarantine
move, child launch, and final verification. Windows uses a named mutex scoped
to the normalized run path; Linux uses an advisory `flock` on the lock file.
That closes the check-to-mutation race between cooperating local BenchHandoff
writers. It does not prevent another program, privileged process, or hostile
filesystem writer from changing bytes directly. See
[LIMITATIONS.md](LIMITATIONS.md).

The canonical lock record is named
`.<run-name>.benchhandoff-writer-lock.json` beside the run directory and records
the owner PID, supported process-start token, normalized run path, and a random
nonce. Clean exit removes only the exact file object and bytes acquired by that
writer. A hard process exit releases the kernel guard but can leave the record;
automatic `start` and `resume` still stop instead of guessing that ownership is
stale.

Orphan recovery is an explicit two-step control-plane action. First,
`inspect-writer-lock` reads at most 4096 bytes, requires the exact canonical
schema, samples owner liveness twice, and emits a decision SHA-256. It recommends
recovery only when the recorded PID is definitely dead or a live PID has a
stable different process-start token. Live, unknown, unstable, or
identity-unverifiable owners are refused. Recovery has no age timeout.

```powershell
$lockDecision = python -m benchhandoff inspect-writer-lock runs\recovery |
  ConvertFrom-Json
if ($lockDecision.action -ne "recover-orphan") {
  throw "writer lock is not proven orphaned: $($lockDecision.reason)"
}
python -m benchhandoff recover-writer-lock runs\recovery `
  --expected-decision-sha256 $lockDecision.decision_sha256
```

`recover-writer-lock` reacquires the kernel guard, recomputes the exact decision,
hard-links the original record to a SHA-named sibling tombstone, verifies that
both names identify the same file object and bytes, then removes only the source
name. It does not resume the run; inspect and resume separately afterward. A
crash after tombstone creation but before source unlink is safely resumable.
Malformed locks, unexpected hard links, foreign tombstones, stale decisions,
and ambiguous ownership remain blocked for manual evidence review.

Commands should be idempotent and avoid undeclared side effects. A task that
completed externally but was not durably recorded is deliberately retried. A
hard crash in a child-launch or atomic-write window can still leave a
fail-closed run-evidence state that requires manual review or abandonment; lock
recovery does not claim that retrying the task itself is safe.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Completed or verified |
| `2` | Command-line syntax error |
| `10` | Invalid configuration or filesystem boundary |
| `20` | Child task failed closed |
| `30` | Evidence or operational state could not be safely interpreted |

## Reproduce the synthetic evidence package

From a clean checkout, create a unique parent outside the repository and run
the single cross-platform entrypoint:

```powershell
$parent = New-Item -ItemType Directory -Path (
  Join-Path ([IO.Path]::GetTempPath()) ("benchhandoff-" + [guid]::NewGuid())
)
$package = Join-Path $parent.FullName "package"
$expectedCommit = (git rev-parse HEAD).Trim()
$env:PYTHONPATH = "src"
python benchmarks\synthetic\reproduce.py --output-dir $package
if ($LASTEXITCODE -ne 0) { throw "package generation failed" }
python benchmarks\synthetic\reproduce.py `
  --verify-dir $package `
  --expected-commit $expectedCommit
```

It produces two raw records, a bounded summary, `SHA256SUMS.txt`, and a final
`PACKAGE_COMPLETE.json`; it refuses dirty source, an existing target, a
nonempty or linked parent, and output inside the checkout. The same entrypoint
then verifies the exact bounded topology, strict records, hashes, completion
binding, invariants, and expected commit without mutating the package. See
[docs/REPRODUCING.md](docs/REPRODUCING.md) for Linux syntax, package contents,
source-authentication limit, and the independent-reproduction claim boundary.

The fixed pipeline has 12 sequential tasks; task 6 fails on its first call. The
asserted scenario executes 18 child processes for a naive full restart and 13
for BenchHandoff resume, repeating five versus zero successful tasks, while
ending at the same final output hash. These are deterministic child-work counts,
not wall-clock, production, or third-party performance evidence. The focused
one-task diagnostic additionally changes a reviewed partial output, proves the
stale decision is rejected without changing state, events, quarantine, output,
or attempt count, then refreshes the decision and completes. It remains at
`benchmarks/synthetic/run_benchmark.py`.

A separate deterministic contention benchmark starts from one failed attempt,
holds the run's writer lock in a second Python process, and attempts one
competing resume:

```powershell
python benchmarks\synthetic\run_writer_contention.py `
  --output writer-contention.json
```

It asserts two participating processes, one rejected competing resume, zero
changed run-evidence files, an unchanged partial output and attempt count, then
cleanly releases the holder and completes attempt 2. This is local cooperative
filesystem behavior, not timing, production reliability, network-filesystem,
distributed-scheduler, or hostile-writer evidence.

The orphan-recovery diagnostic uses a second process that acquires the same
writer lock and then calls `os._exit(0)`. It performs two identical read-only
lock inspections, applies the exact bound recovery, proves zero run-evidence or
partial-output changes and an unchanged attempt count, preserves the source
record as a tombstone, then separately performs bound run resume and verify:

```powershell
python benchmarks\synthetic\run_writer_recovery.py `
  --output writer-recovery.json
```

This measures deterministic local control-plane state only. It is not evidence
that retrying an arbitrary child is safe, nor production reliability, hostile-
writer protection, distributed coordination, or external adoption.

## Tests

```powershell
$env:PYTHONPATH = "src"
python tools\verify_external_evidence.py
python -m unittest discover -s tests -v
```

## Public evidence and independent reproduction

The CI definition separates an eight-job operating-system/Python test matrix, a
single canonical synthetic-evidence job, and a package job that builds once,
checks both distributions, installs the exact wheel outside the checkout, runs
the failure-to-resume example, and uploads only those tested bytes. It is
license-gated and remains a proposed plan until public workflow URLs exist.

After publication, structured issue forms can record reproducible bugs and
bounded independent reproduction attempts, independent use, institutional
adoption, and third-party review. An opened form does not create a count.
Only a human-reviewed record merged into the canonical ledger does.

The current verified public baseline is zero independent reproductions, zero
independent users, zero institutional adopters, and zero third-party reviews.
Repository views, stars, forks, downloads, installs, self-authored examples,
and CI runs are not described as external adoption. See
[docs/EXTERNAL_EVIDENCE.md](docs/EXTERNAL_EVIDENCE.md) for the taxonomy,
review, deduplication, retraction, and validator boundary.

## Project documents

- [Evidence format](docs/EVIDENCE_FORMAT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Recovery example](examples/recovery_pipeline/README.md)
- [Engineering case study](docs/ENGINEERING_CASE_STUDY.md)
- [External evidence ledger](docs/EXTERNAL_EVIDENCE.md)
- [Limitations](LIMITATIONS.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Support](SUPPORT.md)
- [Release process](docs/RELEASING.md)
- [Changelog](CHANGELOG.md)
