# BenchHandoff v0.5.0

[![CI](https://github.com/tiramitree/benchhandoff/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tiramitree/benchhandoff/actions/workflows/ci.yml)
[![AgentRun real kind E2E](https://github.com/tiramitree/benchhandoff/actions/workflows/agentrun-kind.yml/badge.svg?branch=main)](https://github.com/tiramitree/benchhandoff/actions/workflows/agentrun-kind.yml)

> **Release scope:** Version 0.5.0 is a GitHub-only early version line when
> published. A source commit alone is not a release. It is not published to a
> package registry and does not establish production support, compatibility,
> independent review, or adoption.

BenchHandoff is a narrow evidence engine for resuming a flat, sequential batch
of expensive commands. The Python CLI runs locally. Version 0.4 also includes
an optional early-alpha Kubernetes `AgentRun` controller that schedules the
same version 3 protocol as deterministic Jobs over one PVC; it does not turn
the engine into a general distributed scheduler.

The engine fingerprints the suite and declared inputs, records per-task logs
and declared-output hashes, and skips a previously completed task only when
those outputs still re-verify. It turns a `suite.toml` into three reviewable
records:

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
conclusion; repeat the check immediately before a package-registry release.
BenchHandoff is licensed under
[Apache License 2.0](LICENSE). Repository publication, CI, downloads, stars,
forks, and maintainer-authored examples are not external-adoption evidence.

BenchHandoff is not an experiment tracker, DAG workflow engine, distributed
scheduler, sandbox, cryptographic attestation service, or guarantee of full
reproducibility.

## What v0.5 adds

Version 0.5 changes the reference manager deployment from one replica to a
fixed pair. Both managers use client-go leader election over the namespaced
`coordination.k8s.io/v1` Lease
`benchhandoff-system/agentrun-controller.benchhandoff.dev`. The fixed timing is
a 15-second Lease duration, 10-second renew deadline, and 2-second retry
period. `LeaderElectionReleaseOnCancel` is `false`, so an intentional shutdown
does not voluntarily shorten the observation window by releasing the Lease.
The exact Lease is precreated. Its namespaced Role is restricted by
`resourceNames` and allows only `get` and `update` on that one object. It does
not allow Lease create, list, watch, patch, delete, access to another Lease in
the namespace, or cross-namespace Lease reads.

Version 0.5 also narrows two controller ambiguity windows:

- after a Job create attempt, including `AlreadyExists`, the reconciler uses
  uncached API reads to require one deterministic action Job, one matching
  server-assigned UID, the complete registered Job template, and a non-ambiguous
  Pod set before binding status; an unknown create error is retried through a
  fresh reconcile and never causes a second Job name; and
- a status-update conflict discards the stale status candidate and schedules a
  fresh reconcile. A later pass adopts only the same validated Job UID; a
  different bound UID blocks the run.

The registered v0.5 gate requires a clean exact source commit and one
disposable single-node kind cluster with two running Ready manager Pods. For
each paused synthetic `start` and `resume` runner, it binds one stable Lease
resource version to both manager names and UIDs, cordons the node, and deletes
the exact holder through a UID-preconditioned API request. Only the previously
observed non-holder Pod, with its original UID, may acquire exactly the next
Lease transition. The gate then uncordons the node and restores two Ready
replicas. Across each takeover, the measured before/after Job and Pod names,
UIDs, and cardinalities must remain one exact object each.

The gate writes one bounded `takeover-evidence.json` containing those measured
fields, binds it with one `SHA256SUMS` entry, rejects any extra or non-regular
entry, and applies the repository privacy scanner to both files. The workflow
uploads those exact two files only for a trusted repository push or manual
dispatch after success; pull-request executions do not publish an official
artifact.

This is a fixed active/passive takeover experiment with the API server and its
storage continuously available. It is not strict fencing, network-partition
safety, arbitrary Pod recovery, multi-node or multi-cluster availability,
exactly-once execution, or production high availability.

## What v0.4 adds

`control.benchhandoff.dev/v1alpha1` defines one namespaced `AgentRun`. Its
immutable execution spec binds a PVC, normalized suite path, exact suite
SHA-256, digest-pinned runner image, and bounded deadline. The Go manager
creates one deterministic `start`, `resume`, or `verify` Job at a time and
accepts only an exact owner UID, canonical execution-spec digest, audited Job
template, live Job UID, single owned Pod, and bounded termination message.

A failed start publishes a deterministic resume-decision SHA-256 and stops at
`AwaitingApproval`. Copying that exact digest into the write-once spec field
allows a bound resume; a distinct verify Job must then re-open the bundle and
return the same run and bundle identities before the resource becomes
`Succeeded`. Manager restart/adoption re-lists the live Job and refuses
missing, additional, replaced, foreign, or template-drifted objects.

Runner Pods use no service-account token or service links, run as non-root
UID/GID 65532 with a read-only root filesystem, drop every capability, disable
privilege escalation, and use runtime-default seccomp. These settings narrow
the registered Job template; they are not a workload sandbox, tenant-isolation
claim, or proof that the runner image is safe.

Kubernetes transition rules reject a wrong first approval and make the accepted
digest write-once. Rules using `oldSelf` are skipped on CREATE, so a prefilled
approval can pass admission; the controller detects it before creating a Job
and moves the run to `Blocked` with reason `PreseededApproval`. The decision
digest is review data, not a credential or authorization token.

The public pinned single-node kind gate covers deliberate failure, exact
approval, bound resume, fresh verify, manager restart/adoption, a declared
suite-digest mismatch, wrong approval, duplicate Pod rejection, Go race tests,
and bounded cleanup.
It does not establish high availability, exactly-once side effects,
multi-cluster coordination, production reliability, independent review, or
external adoption.

See [AgentRun controller](docs/AGENTRUN_CONTROLLER.md) for the API, bindings,
reproduction command, observed public run, and explicit non-claims.

## What v0.3 adds

Suite schema version 3 keeps the version 2 execution-context and process-family
controls, then adds a reviewed closed-world manifest for one dedicated
`workspace.root`. Before a run is created, before every task launch, after the
leader and its in-scope process family stop, during recovery, before bundling,
and during `verify`, BenchHandoff compares the bounded directory topology and
ordinary-file primary-stream bytes under that root with the state derivable
from the manifest and already sealed outputs. Missing, extra, changed, linked,
reparse, hard-linked, cross-device, or unsupported entries fail closed.
The bound state is directory topology plus ordinary-file primary-stream bytes.
It does not bind mode, owner, timestamps, ACLs, extended attributes, NTFS
alternate data streams, sparse-file layout, or other unlisted metadata.

These are discrete observations, not continuous monitoring. Version 3 neither
prevents a child from writing outside `workspace.root` nor observes such writes.
It is not a sandbox or hostile-writer boundary. In particular, a same-device
bind mount can retain the same device identifier and is not detected as a
cross-device boundary.

`snapshot-workspace` writes a new review candidate outside `workspace.root`;
the destination must start absent. If publication or re-verification fails, the
candidate is intentionally retained for review rather than silently deleted.
The canonical manifest contains relative paths, entry kinds, file sizes, and
SHA-256 content identities, so it can reveal project structure, filenames,
sizes, and content fingerprints. Raw run evidence and writer-lock records can
still contain absolute local paths.

Recovery uses atomic no-replace quarantine moves on Windows, Linux, and macOS;
an unavailable primitive or any other platform fails closed. macOS remains
unsupported for child execution. A hard runner crash can leave a durable
`running` attempt without a terminal observation; recovery then records a new
recovery-time workspace observation before quarantine. That observation cannot
be interpreted as the exact tree at crash time.

Versions 1 and 2 retain their existing parsers, evidence shapes, task roots,
and execution behavior. Version 3 is opt-in and strict. See
[Closed-world workspace integrity](docs/CLOSED_WORLD_WORKSPACE_INTEGRITY.md)
for the protocol and claim boundary.

## What v0.2 adds

Suite schema version 2 binds a real descriptor file, the resolved executable's
content and hashed path identity, a non-inheriting launch-environment policy,
and the selected process-family backend. Those identities are checked before
launch, after leader exit, and before resume mutation.

Windows uses an anonymous kill-on-close Job Object and assigns a suspended
leader before its first instruction can run. Linux uses a new session/process
group with cooperative TERM/KILL cleanup and resume-time membership checks.
Every version 2 attempt must prove its in-scope process family empty before any
output is hashed. A surviving descendant therefore fails the attempt and
cannot be sealed into a bundle.

This is evidence-bound launch context plus process-family lifecycle control.
It is not a VM/container snapshot system, cgroup implementation, package or
driver inventory, security sandbox, or hostile-code boundary. The Linux
process-group backend can be escaped by a descendant that creates a new
session. Exact design and claim boundaries are in
[the version 2 protocol note](docs/EXECUTION_CONTEXT_AND_PROCESS_SCOPE.md).

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

- Python 3.11 or newer. The v0.4 code-bearing public CI covered
  CPython 3.11 through 3.14 on Ubuntu 24.04 and Windows Server 2025.
- Windows or Linux for child execution. Linux requires `/proc` so the runner can
  bind a PID to a stable process-start identity and inspect version 2 process
  groups.
- The Python engine has no third-party runtime packages.
- A suite directory and a separate run-evidence directory. Version 3 also
  requires one dedicated `workspace.root` and a reviewed manifest outside that
  root.
- The suite, every output's existing parent, and the run/quarantine directory
  must be on the same filesystem so quarantine moves cannot cross devices.
- The optional controller uses Go 1.26 and pinned Kubernetes/client libraries.
  Its one observed integration target is kind v0.32.0 with Kubernetes v1.36.1.
  It requires a writable PVC populated with version 3 suites and a digest-pinned
  runner image. No controller image or production installer is published.

macOS and other operating systems are not supported for execution. A new start
rejects them before creating the run directory; resume rechecks support before
any child launch. The optional package build uses setuptools. Running from
source needs no install.

## Validation status

This source tree does not carry forward validation identifiers from
superseded history. Checked-in tests and workflows define reproducible
procedures; no current-tip public CI or release evidence is claimed until an
exact-commit run completes.

The ordinary
[CI workflow](https://github.com/tiramitree/benchhandoff/actions/workflows/ci.yml)
defines the operating-system/Python matrix, synthetic evidence regeneration,
privacy and license gates, and exact-distribution smoke. The
[real-kind workflow](https://github.com/tiramitree/benchhandoff/actions/workflows/agentrun-kind.yml)
defines the bounded two-manager takeover fixture. A workflow file or an
unrelated successful run is not evidence for this source revision.

The registered kind fixture checks a deliberate version 3 failure, exact
approval, resume, fresh verification, manager takeover, stable live Job/Pod
identity, and bounded cleanup in one disposable single-node environment.
These are maintainer-operated synthetic checks. They do not establish
independent reproduction, production reliability, strict fencing,
exactly-once behavior, general Kubernetes compatibility, external use, or
adoption.

## GitHub releases

Versions 0.1 through 0.4 are GitHub-only early releases. The authoritative
current tag and asset state is shown on the
[Releases page](https://github.com/tiramitree/benchhandoff/releases).
This document intentionally omits superseded commit, run, artifact, asset,
size, and digest identifiers; they do not validate a rewritten tag.

Version 0.5 is a GitHub-only early release only when its current annotated tag
peels to a commit that completed the registered gates. The Go manager, CRD,
reference Kustomize manifests, and kind gate remain source-only; no controller
image, Helm chart, package-registry publication, production support,
independent reproduction, external use, or adoption is asserted.

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

Version 3 runs every task relative to one dedicated workspace and binds a
canonical manifest stored outside that workspace. Generate a new candidate with
`snapshot-workspace`, review its exact bytes and metadata disclosure, then put
its SHA-256 and byte size in the suite:

```toml
version = 3
name = "closed-world-copy"

[context]
path = "context.json"
media_type = "application/vnd.example.context.v1+json"
digest = "sha256:<digest of workspace/context.json>"
size = 80

[workspace]
root = "workspace"
manifest = "workspace.snapshot.json"
digest = "sha256:<digest of workspace.snapshot.json>"
size = 512
policy = "closed-world-primary-stream-v1"

[[task]]
id = "copy-upper"
argv = ["python", "copy_upper.py", "input.txt", "output.txt"]
inputs = ["context.json", "copy_upper.py", "input.txt"]
outputs = ["output.txt"]
```

The manifest must start as a snapshot of the bounded directory topology and
ordinary-file primary streams under `workspace`, with every declared output
absent. Both task paths and a relative executable path are resolved against
`workspace.root`; the manifest path is resolved against the suite directory
and must remain outside the workspace.
Use `inspect-workspace` for a launch-free validation. The complete workflow and
bounds are in
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](docs/CLOSED_WORLD_WORKSPACE_INTEGRITY.md).

Version 2 adds a byte-verified context descriptor. Its path must also be a seed
task input:

```toml
version = 2
name = "context-bound-copy"

[context]
path = "context.json"
media_type = "application/vnd.example.context.v1+json"
digest = "sha256:<digest of context.json>"
size = 80

[[task]]
id = "copy-upper"
argv = ["python", "copy_upper.py", "input.txt", "output.txt"]
inputs = ["context.json", "copy_upper.py", "input.txt"]
outputs = ["output.txt"]
```

The executable must be a portable bare name or suite-relative path; versions 2
and 3 reject an absolute `argv[0]`. A runnable version 2 fixture is under
[`examples/context_bound`](examples/context_bound).

Version 1 remains readable and executable with its original evidence shape:

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

The parser bounds the suite to 256 KiB, 64 tasks, and 512 total declared
input/output references. Each task is bounded to 128 arguments, 64 inputs, and
64 outputs; additional byte and portable-path limits are enforced before the
run directory is created.

All commands receive:

- `BENCHHANDOFF_RUN_ID`
- `BENCHHANDOFF_TASK_ID`
- `BENCHHANDOFF_ATTEMPT`

Version 1 additionally inherits the caller environment. Versions 2 and 3 do
not: they pass only these control variables plus hashed-and-bound `SystemRoot` on
Windows. In particular they do not pass `PATH`, tokens, user-home variables, or
arbitrary caller configuration.

Declare scripts, configs, datasets, and other material dependencies as inputs
when their identities need to be covered by evidence.

## Failure and resume

`start` exits `20` when a child fails. Its stdout and stderr remain in the run
directory, no bundle exists, and partial regular-file outputs remain unverified.
`resume`:

1. verifies the suite, seed inputs, plan, event chain, and completed prefix;
2. refuses to mutate evidence if the next-attempt budget is exhausted;
3. refuses to proceed while a recorded child or version 2 cooperative process
   group may still be alive, or a launch identity remains unresolved;
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
python tools\verify_license_state.py --require-final
python tools\verify_external_evidence.py
python -m unittest discover -s tests -v
```

## Public evidence and independent reproduction

The public CI separates an eight-job operating-system/Python test matrix, a
single canonical synthetic-evidence job, and a package job that builds once,
checks both distributions, installs the exact wheel outside the checkout, runs
the failure-to-resume example, and uploads only those tested bytes. It is
license-gated. The reproducible fixture records 18 versus 13 synthetic child
calls, 5 versus 0 duplicate successful calls, and identical final output bytes
for restart-from-zero versus evidence-verified resume. Those are deterministic
synthetic work counts, not elapsed-time speed or a real-workload performance
result. Validate them against the exact checked-out revision; do not transfer
them from another run or tag.

Structured issue forms can record reproducible bugs and
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
- [AgentRun Kubernetes controller](docs/AGENTRUN_CONTROLLER.md)
- [Version 2 execution context and process scope](docs/EXECUTION_CONTEXT_AND_PROCESS_SCOPE.md)
- [Version 3 closed-world workspace integrity](docs/CLOSED_WORLD_WORKSPACE_INTEGRITY.md)
- [Recovery example](examples/recovery_pipeline/README.md)
- [Engineering case study](docs/ENGINEERING_CASE_STUDY.md)
- [External evidence ledger](docs/EXTERNAL_EVIDENCE.md)
- [Limitations](LIMITATIONS.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Support](SUPPORT.md)
- [Apache-2.0 license](LICENSE)
- [Release process](docs/RELEASING.md)
- [Changelog](CHANGELOG.md)
