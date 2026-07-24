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

The exact name was checked against PyPI, npm, crates.io, and GitHub on
2026-07-24; repeat that check immediately before publication. This repository
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
8. uses per-file same-directory atomic replacement for state and the
   content-monotonic event chain; and
9. re-hashes the complete declared evidence set and rejects unexpected run-root
   entries during `verify`.

These guarantees apply only to declared files. BenchHandoff is not a sandbox,
and its per-file atomic writes are not a cross-file transaction or a power-loss
guarantee. See [LIMITATIONS.md](LIMITATIONS.md).

## Requirements and implemented execution targets

- Python 3.11 or newer.
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

As of 2026-07-24, the current local Windows/Python 3.12.13 suite ran 81 tests:
79 passed and 2 symlink-creation tests were skipped because this Windows account
lacks that privilege. There were no failures or errors. The checked-in CI matrix
covers Ubuntu and Windows with Python 3.11 and 3.12, but it is only a proposed
test plan until an online run exists. No production, third-party, or external
adoption claim is made. See [VALIDATION_20260724.md](VALIDATION_20260724.md).

## Five-minute quickstart

From the repository root in PowerShell:

```powershell
Copy-Item -LiteralPath examples\basic -Destination demo -Recurse
New-Item -ItemType Directory -Path runs
$env:PYTHONPATH = "src"
python -m benchhandoff start demo\suite.toml --run-dir runs\basic
python -m benchhandoff verify runs\basic
```

The task writes `demo/output.json`; the run evidence is written separately under
`runs/basic`. Both paths must start absent.

Equivalent commands on Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
cp -R examples/basic demo
mkdir -p runs
PYTHONPATH=src python -m benchhandoff start demo/suite.toml --run-dir runs/basic
PYTHONPATH=src python -m benchhandoff verify runs/basic
```

Activating the virtual environment matters for this example because the suite
invokes `python` by name.

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

Commands should be idempotent and avoid undeclared side effects. A task that
completed externally but was not durably recorded is deliberately retried. A
hard crash in a launch or atomic-write window can instead leave a permanent
fail-closed guard that requires manual evidence review or abandonment; v0.1
does not guess that retry is safe.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Completed or verified |
| `2` | Command-line syntax error |
| `10` | Invalid configuration or filesystem boundary |
| `20` | Child task failed closed |
| `30` | Evidence or operational state could not be safely interpreted |

## Reproduce the 12-task synthetic comparison

```powershell
$env:PYTHONPATH = "src"
python benchmarks\synthetic\run_pipeline_comparison.py
```

The fixed pipeline has 12 sequential tasks; task 6 fails on its first call. The
asserted scenario executes 18 child processes for a naive full restart and 13
for BenchHandoff resume, repeating five versus zero successful tasks, while
ending at the same final output hash. These are deterministic child-work counts,
not wall-clock, production, or third-party performance evidence. The focused
one-task diagnostic remains at `benchmarks/synthetic/run_benchmark.py`.

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

See [docs/EVIDENCE_FORMAT.md](docs/EVIDENCE_FORMAT.md) for the record model and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the execution boundary.
