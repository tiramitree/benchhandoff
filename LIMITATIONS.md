# Limitations

BenchHandoff v0.2 is a narrow local run-evidence CLI, not a security sandbox or a
distributed workflow engine.

## Implemented execution targets

- Child execution targets Windows and Linux only. Version 0.1 has a complete
  public cross-platform matrix; version 0.2 remains a candidate until its own
  exact commit completes that matrix.
- Linux requires readable `/proc` process identities and group membership.
  macOS and other operating systems do not have a supported start-token
  implementation. A new start rejects them before creating the run directory;
  resume rechecks support before any child launch.
- A checked-in workflow is not evidence that a new change passed online. Do not
  transfer the version 0.1 run result to the version 0.2 candidate.

## Execution isolation

- Version 1 children inherit the caller's environment. Version 2 children do
  not: they receive only three derived runner variables and Windows
  `SystemRoot`. Both versions inherit the caller's operating-system
  permissions.
- There is no network, syscall, GPU, filesystem-write, resource, or secret
  isolation.
- The runner observes declared inputs and outputs only. A command can create or
  mutate undeclared files or contact services.
- Version 1 manages only the direct child and does not hash `argv[0]`
  automatically. Relevant scripts, configs, and other material dependencies
  must be declared in `inputs`.
- Version 2 hashes the resolved executable and its normalized-path identity.
  It controls ordinary descendants through a Windows Job Object or cooperative
  Linux process group, and requires that scope to be empty before output
  hashing. Windows mechanisms that create work outside the Job and POSIX
  descendants that call `setsid()`/`setpgid()` remain outside that scope.
- Version 2 binds and byte-checks an opaque descriptor file. It does not parse
  or instantiate a VM/container snapshot, verify packages or drivers, or prove
  that an external object named by the descriptor exists.

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
  blocks automatic resume and verify. BenchHandoff never deletes that evidence
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
- On a Windows version 2 hard runner exit, the anonymous Job's
  `KILL_ON_JOB_CLOSE` behavior terminates in-Job members. On Linux, a hard
  runner exit does not terminate the cooperative process group. Resume checks
  the persisted group and refuses while a member is present or membership is
  unknown. Neither behavior covers a process that escaped its assigned scope.
- `state.json`, `events.jsonl`, and `bundle.json` use per-file same-directory
  atomic replacement. They are not one cross-file atomic transaction and do
  not guarantee behavior across sudden power loss, storage-controller failure,
  or a filesystem that violates ordinary replace/fsync semantics.
- Event transitions use a pending intent plus a content-monotonic hash chain and
  can reconcile the two explicitly modeled interrupted-write states. This is
  not full event sourcing or universal crash recovery.
- `events.jsonl` is rewritten in full for each transition. Work is linear in the
  existing log per transition and quadratic over a very long run. The protocol bounds
  it to 64 MiB, 100,000 records, and 256 KiB per record.
- `inspect` decisions are optional content bindings. Plain `resume` remains
  available and can reconcile modeled pending events; policy enforcement
  therefore depends on the caller supplying `--expected-decision-sha256`.
- A bound resume rechecks its decision immediately before the first transition.
  The digest is not a secret, signature, authorization service, or lock.
  `start` and `resume` separately hold a cooperative local writer lock across
  their complete mutation path, so two BenchHandoff processes cannot both
  cross the check-to-mutation window for one run.
- The sibling writer-lock record uses local filesystem `O_EXCL` creation.
  Windows mutation/recovery also holds a named mutex keyed by the normalized
  run path; Linux holds advisory `flock` on the lock file. These kernel guards
  are released automatically when a process exits. They are not remote leases,
  heartbeats, fencing tokens, network-filesystem proof, distributed storage,
  remote workers, scheduler locks, or a multi-host clock model.
- A normal return or handled failure removes only the exact lock file object
  and bytes acquired by that process. A hard exit can leave the canonical
  record after the kernel guard is released. Automatic `start` and `resume`
  remain blocked until a separate recovery command succeeds.
- `inspect-writer-lock` is read-only and recommends recovery only for a
  definitely dead owner or a stable live PID whose process-start token differs
  from the recorded token. Unknown liveness, a missing token for a live PID,
  changing observations, malformed/noncanonical/oversized records, and
  unexpected hard links fail closed. Wall-clock age is never evidence of
  orphanhood.
- `recover-writer-lock` requires the exact current decision SHA-256, reacquires
  the local kernel guard, preserves the original record as a hard-linked
  SHA-named tombstone, and unlinks only the source name. The tombstone remains
  outside the run bundle and can expose the absolute run path and owner PID.
  The command does not prove that retrying a child is safe and does not resume
  the run.
- A crash after tombstone creation but before source unlink is resumable when
  both names still identify the exact same file object. A crash after source
  unlink may complete recovery without returning CLI output. Malformed lock
  creation, hostile concurrent mutation, unsupported hard-link semantics, or
  ambiguous ownership still require manual review or abandonment.
- The lock coordinates cooperating local BenchHandoff entrypoints. It is not a
  defense against a privileged or hostile process that edits or removes the
  lock, tombstone, or run evidence directly.

## Evidence and reproducibility

- A SHA-256 match proves byte identity, not scientific validity, semantic
  correctness, provenance ownership, or absence of malicious content.
- The top-level environment record is descriptive and incomplete. Version 2
  additionally proves its non-inheriting variable policy, hashes the one
  Windows static value it passes, and binds the descriptor/executable/scope
  identities. It still does not capture packages, drivers, containers,
  hardware, locale, system libraries, or descriptor semantics.
- `plan.json` and the transient resume decision record absolute run, suite, and
  suite-file paths, and `bundle.json` records the absolute suite-file path.
  Those values can expose a local username, drive letter, mount point, or other host layout. Treat run
  evidence as potentially identifying. Editing records in place invalidates
  their evidence bindings and is not a redaction mechanism.
- Logs may contain data emitted by commands. BenchHandoff does not redact
  secrets.
- Bundles are unsigned local manifests. A party able to rewrite the entire run
  directory can fabricate a self-consistent replacement.
- v0.2 has no archive export, signature, transparency log, remote attestation,
  cost accounting, or benchmark comparison engine.

## Claim boundary

Current validation is local and synthetic. No claim is made about production
use, external users, independent reproduction, real-workload performance,
public release, or compatibility with a specific embodied-simulation stack.
