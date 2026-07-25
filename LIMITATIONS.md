# Limitations

BenchHandoff v0.1 is a narrow local run-evidence CLI, not a security sandbox or a
distributed workflow engine.

## Implemented execution targets

- Child execution targets Windows and Linux only. Those implementations exist,
  but cross-platform support is not claimed until the public CI matrix has
  completed successfully.
- Linux requires a readable `/proc` process identity. macOS and other operating
  systems do not have a v0.1 start-token implementation and fail closed after
  launch rather than proceeding without a stable child identity.
- The checked-in CI matrix is not evidence of an online CI run. Current observed
  compatibility preflight covers local Windows with CPython 3.11.15, 3.12.13,
  3.13.14, and 3.14.6. No Linux result is claimed before public CI.

## Execution isolation

- Child processes inherit the caller's environment and operating-system
  permissions.
- There is no network, syscall, GPU, filesystem-write, resource, or secret
  isolation.
- The runner observes declared inputs and outputs only. A command can create or
  mutate undeclared files, contact services, or launch descendants.
- Executables named in `argv[0]` are not hashed automatically. Relevant scripts,
  configs, and other material dependencies must be declared in `inputs`.

Use an external container, VM, account boundary, or sandbox when hostile code is
in scope.

## Filesystem model

- Content hashes and ordinary-file checks narrow accidental evidence drift but
  do not defend against a privileged concurrent attacker.
- There are unavoidable check/open race windows around paths created by child
  processes.
- Symlink traversal is rejected. Platform-specific reparse-point behavior,
  especially Windows junction edge cases, has not received a complete
  adversarial audit.
- Outputs are written by the child into the suite tree. BenchHandoff verifies
  them after exit; it does not force the child to publish atomically.
- Recovery moves unverified regular outputs into `quarantine/`. Directories,
  links, devices, and other non-regular partial outputs stop recovery.
- Cross-filesystem quarantine is unsupported. The suite, every output's
  existing parent, and the run/quarantine directory must share one filesystem.
- The run root has an exact topology. An unexpected root entry, including a
  same-directory atomic-write `.tmp` left by a hard process kill, permanently
  blocks automatic resume and verify. v0.1 never deletes that evidence
  automatically.

## Recovery and atomicity

- Resume is at-least-once, not exactly-once. If a child completed but the runner
  died before terminal state was committed, its declared outputs are
  quarantined and the task is retried.
- Tasks must be idempotent and must not depend on undeclared side effects.
- A durable launch guard is written before `Popen`. If the runner dies between
  launch and durable child identity, or cannot confirm shutdown after a control
  failure, future resume is refused. Manual evidence review or abandonment is
  required; the runner does not assume that the child is dead.
- `state.json`, `events.jsonl`, and `bundle.json` use per-file same-directory
  atomic replacement. They are not one cross-file atomic transaction and do
  not guarantee behavior across sudden power loss, storage-controller failure,
  or a filesystem that violates ordinary replace/fsync semantics.
- Event transitions use a pending intent plus a content-monotonic hash chain and
  can reconcile the two explicitly modeled interrupted-write states. This is
  not full event sourcing or universal crash recovery.
- `events.jsonl` is rewritten in full for each transition. Work is linear in the
  existing log per transition and quadratic over a very long run. v0.1 bounds
  it to 64 MiB, 100,000 records, and 256 KiB per record.
- `inspect` decisions are optional content bindings. Plain `resume` remains
  available and can reconcile modeled pending events; policy enforcement
  therefore depends on the caller supplying `--expected-decision-sha256`.
- A bound resume rechecks its decision immediately before the first transition.
  The digest is not a secret, signature, authorization service, or lock.
  `start` and `resume` separately hold a cooperative local writer lock across
  their complete mutation path, so two BenchHandoff processes cannot both
  cross the check-to-mutation window for one run.
- The sibling writer lock uses local filesystem `O_EXCL` creation. It is not a
  remote lease, does not expire, and has no heartbeat, fencing token,
  network-filesystem proof, distributed storage, remote worker, scheduler, or
  multi-host clock model.
- A normal return or handled failure removes only the exact lock bytes acquired
  by that process. A hard process exit can leave an orphan lock. Future
  mutation remains blocked until a human establishes that no writer is active
  and handles the exact sibling file; v0.1 does not auto-break it.
- The lock coordinates cooperating BenchHandoff entrypoints. It is not a
  defense against a privileged or hostile process that edits or removes the
  lock or run evidence directly.

## Evidence and reproducibility

- A SHA-256 match proves byte identity, not scientific validity, semantic
  correctness, provenance ownership, or absence of malicious content.
- The environment record is descriptive and incomplete. It does not capture
  packages, drivers, containers, hardware, locale, or environment variables.
- `plan.json` and the transient resume decision record absolute run, suite, and
  suite-file paths, and `bundle.json` records the absolute suite-file path.
  Those values can expose a local username, drive letter, mount point, or other host layout. Treat run
  evidence as potentially identifying. Editing records in place invalidates
  their evidence bindings and is not a redaction mechanism.
- Logs may contain data emitted by commands. BenchHandoff does not redact
  secrets.
- Bundles are unsigned local manifests. A party able to rewrite the entire run
  directory can fabricate a self-consistent replacement.
- v0.1 has no archive export, signature, transparency log, remote attestation,
  cost accounting, or benchmark comparison engine.

## Claim boundary

Current validation is local and synthetic. No claim is made about production
use, external users, independent reproduction, real-workload performance,
public release, or compatibility with a specific embodied-simulation stack.
