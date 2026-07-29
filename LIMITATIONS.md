# Limitations

BenchHandoff v0.5.0 is currently an unreleased candidate. BenchHandoff remains
a narrow run-evidence engine, not a security sandbox, hostile-writer boundary,
or distributed workflow engine. The current completed release is v0.4.0.

## Implemented execution targets

- Child execution targets Windows and Linux only. Version 2 code-bearing commit
  `pre-rewrite-commit-retired` completed the public Ubuntu 24.04
  and Windows Server 2025 matrix across CPython 3.11 through 3.14 in
  [run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
- Linux requires readable `/proc` process identities and group membership.
  macOS and other operating systems do not have a supported start-token
  implementation. A new start rejects them before creating the run directory;
  resume rechecks support before any child launch.
- A checked-in workflow is not evidence that a new change passed online. Do not
  transfer the recorded result to another commit, platform, interpreter,
  workload, or environment. A release tag must point to a commit that completed
  its own release-gated CI.

## Execution isolation

- Version 1 children inherit the caller's environment. Version 2 and 3 children
  do not: they receive only three derived runner variables and Windows
  `SystemRoot`. Both versions inherit the caller's operating-system
  permissions.
- There is no network, syscall, GPU, filesystem-write, resource, or secret
  isolation.
- Versions 1 and 2 observe declared inputs and outputs only. Version 3 also
  observes bounded directory topology and ordinary-file primary-stream bytes
  under its one declared `workspace.root`. Every version can contact services
  or create side effects outside its observed file boundary.
- Version 1 manages only the direct child and does not hash `argv[0]`
  automatically. Relevant scripts, configs, and other material dependencies
  must be declared in `inputs`.
- Version 2 hashes the resolved executable and its normalized-path identity.
  It controls ordinary descendants through a Windows Job Object or cooperative
  Linux process group, and requires that scope to be empty before output
  hashing. Windows mechanisms that create work outside the Job and POSIX
  descendants that call `setsid()`/`setpgid()` remain outside that scope.
- Version 2 preserves the suite's portable `argv[0]` and does not provide
  `PATH`. On Linux, programs that reconstruct their own executable from
  `argv[0]` and `PATH` can therefore fail; CPython `sys.executable` is not
  guaranteed. A task that must relaunch itself needs an explicit declared
  mechanism. `/proc/self/exe` is available only within the supported Linux
  boundary.
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
- Recovery moves unverified regular outputs into `quarantine/`. It uses an
  atomic no-replace rename on Windows, Linux, and macOS so a destination that
  appears during the move is never overwritten. A missing native primitive,
  any other platform, directories, links, devices, and other non-regular
  partial outputs stop recovery. macOS is not a supported child-execution
  target despite having a no-replace move implementation.
- Cross-filesystem quarantine is unsupported. The suite, every output's
  existing parent, and the run/quarantine directory must share one filesystem.
  Version 3 additionally requires every observed workspace entry to report the
  workspace root's device id. A same-device bind mount may therefore evade this
  check and alias storage outside the apparent tree.
- The run root has an exact topology. An unexpected root entry, including a
  same-directory atomic-write `.tmp` left by a hard process kill, permanently
  blocks automatic resume and verify. BenchHandoff never deletes that evidence
  automatically.

## Version 3 workspace boundary

- Version 3 runs task paths relative to a dedicated `workspace.root` and checks
  bounded directory topology and ordinary-file primary-stream bytes under that
  root against a reviewed manifest plus derivable sealed outputs. It does not
  scan the suite directory, run directory, sibling paths, mount sources,
  network services, process memory, or any other side effect as part of that
  workspace claim.
- Observations occur at discrete preflight, pre-launch, post-exit,
  recovery-time, pre-bundle, and verification checkpoints. Mutation between
  checkpoints is not continuously observed or prevented.
- The scanner rejects links, reparse points, hard-linked files, cross-device
  entries, unsupported types, aliases, excessive depth, entries, per-file size,
  and total size. These checks narrow accidental drift; they do not make the
  workspace safe against a privileged or hostile concurrent writer.
- The bound state covers directory topology and ordinary-file primary-stream
  bytes only. It excludes mode, owner, timestamps, ACLs, extended attributes,
  NTFS alternate data streams, sparse-file layout, and unlisted metadata.
- `snapshot-workspace` uses start-absent publication and re-verifies the exact
  candidate. If writing, flushing, closing, or re-verification fails, it leaves
  the candidate in place for ownership and evidence review rather than deleting
  a path another actor may have changed. A later attempt must use a different
  absent path or follow an explicit reviewed cleanup decision.
- The manifest intentionally exposes every relative path, entry kind, file
  size, and file content SHA-256 in the reviewed baseline. Names, topology,
  approximate data volume, and content fingerprints may be sensitive even
  though the manifest contains no absolute workspace path.
- Version 3 adds a cooperative writer lock keyed to the workspace root, in
  addition to the run-directory lock. It coordinates BenchHandoff mutation
  entrypoints that share that workspace; it is not authorization or protection
  against direct filesystem mutation.

## AgentRun controller boundary

- The released v0.4 controller is an optional early-alpha reference control
  plane. Its observed integration target is one manager and one node in kind
  v0.32.0 with Kubernetes v1.36.1. The unreleased v0.5 candidate changes the
  reference deployment to exactly two managers using one namespaced Lease, but
  its real-kind and public-CI gates have not yet passed on an immutable v0.5
  commit. Other Kubernetes releases,
  storage classes, container runtimes, architectures, admission stacks, and
  network policies have not been validated.
- The candidate Lease settings are fixed at a 15-second duration, 10-second
  renew deadline, and 2-second retry period with
  `LeaderElectionReleaseOnCancel=false`. These are coordination parameters, not
  fencing tokens or a proof that an old leader cannot act during a partition.
  The registered gate assumes the API server and its storage remain available.
- The candidate precreates the exact leader-election Lease. Its Role is
  namespaced to `benchhandoff-system`, restricted by `resourceNames`, and
  permits only `get` and `update` on that object. It cannot create or recreate
  the Lease, list or watch Leases, patch or delete it, update another Lease in
  the namespace, or read a Lease in another namespace. The broader
  AgentRun/Job/Pod reference ClusterRole remains separate and still requires
  deployment-specific review.
- An `AgentRun` schedules only strict version 3 suites from one writable PVC.
  The controller neither provisions nor snapshots that PVC and does not fence
  direct writers. A workload with PVC access retains the storage permissions
  assigned by the cluster.
- The execution spec is immutable and its runner image must be digest-pinned.
  Image-manifest identity does not prove publisher identity, source
  reproducibility, vulnerability status, architecture support, or runtime
  safety.
- The resume-decision digest is public review data in status/spec. It is not a
  password, signature, user identity, authorization token, or approval audit
  with a human principal. Cluster RBAC and admission policy decide who may
  create or update an `AgentRun`.
- CEL transition rules using `oldSelf` do not execute on CREATE. A prefilled
  approval can therefore pass CRD admission; the controller blocks it as
  `PreseededApproval` before creating a Job. This is controller-enforced, not a
  CREATE-time admission guarantee.
- The manager uses uncached reads before decisions about live Job/Pod identity,
  but it still depends on API-server availability and the consistency
  guarantees of the Kubernetes API and storage. An API error is retried or
  blocks progress; it is not treated as reliable absence.
- Deterministic Job names and live UID bindings allow released v0.4 to adopt an
  exact Job after one tested manager restart. The v0.5 candidate additionally
  observes the deterministic name and complete action-labelled Job set through
  uncached reads after create or `AlreadyExists`, then accepts only one matching
  server UID and complete audited template. Unknown create errors requeue; they
  are not evidence of absence and do not authorize a second Job name.
- A candidate status-update conflict discards the stale object and schedules a
  fresh reconcile. The controller does not blindly retry a stale status patch.
  A different Job UID from the one already bound in status blocks the run.
- The create/`AlreadyExists`, committed-response-loss, and status-conflict
  windows are exercised by bounded fake-client fault-injection unit tests. The
  real-kind takeover gate begins only after `activeJobRef`, its Job, and its Pod
  are established; it tests continuity across holder deletion, not a leader
  exit inside those earlier windows.
- The registered v0.5 gate starts only from a clean exact commit. While a
  paused synthetic `start` Job is live, and again for `resume`, it binds one
  stable Lease version to both manager Pod UIDs, cordons the single node,
  UID-precondition deletes the holder, and accepts only the pre-existing
  non-holder Pod acquiring exactly the next transition. It then uncordons the
  node and requires the measured Job/Pod identities and counts to remain one.
  Until that real-kind gate and public CI pass on an immutable commit, these
  are implemented test conditions, not observed takeover evidence.
- Even after those fixed gates pass, they do not establish strict fencing,
  availability during network partitions, arbitrary Pod recovery, multi-node
  or multi-cluster availability, exactly-once execution, or production high
  availability.
- Each Job has one completion, one worker, zero retries, and one active
  deadline. Kubernetes or node failure can still interrupt work. Deleting or
  replacing an `AgentRun`, Job, Pod, PVC, or its evidence is outside the
  automatic recovery contract.
- The manager accepts one bounded termination message from one Job-owned Pod.
  Kubernetes status is not a signed or remotely attested channel, and a party
  with sufficient cluster privileges can alter the objects the controller
  trusts.
- The runner's non-root UID, read-only root, dropped capabilities, disabled
  service-account token, and seccomp profile narrow the generated template.
  They do not restrict PVC writes, network access, node/kernel attacks,
  workload side effects, or a hostile image.
- The checked-in Kustomize manager image is an E2E placeholder. There is no
  published controller image, Helm chart, upgrade/migration mechanism,
  production configuration, compatibility matrix, or support commitment.
- The public v0.4 kind test uses synthetic local fixtures and one disposable
  registry. The candidate v0.5 gate would additionally produce one bounded
  privacy-gated takeover record and checksum, but it has not yet passed on an
  immutable v0.5 commit. Neither
  gate measures throughput, latency, scale, noisy-neighbor behavior,
  long-duration stability, production recovery, or real model/agent quality.

## Recovery and atomicity

- Resume is at-least-once, not exactly-once. If a child completed but the runner
  died before terminal state was committed, its declared outputs are
  quarantined and the task is retried.
- Tasks must be idempotent and must not depend on undeclared side effects.
- Version 3 normally records `workspace_after` only after the leader has exited
  and the version 2 process scope is empty. A hard runner crash can instead
  leave the attempt durably `running` without that terminal observation.
  Recovery then observes the current workspace immediately before quarantine
  and records that recovery-time view. It cannot prove which bytes existed at
  the instant of the crash, and a previously recorded observation that no
  longer matches is refused rather than rewritten.
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
- The top-level environment record is descriptive and incomplete. Versions 2
  and 3 additionally prove their non-inheriting variable policy, hash the one
  Windows static value they pass, and bind the descriptor/executable/scope
  identities. Version 3 additionally binds workspace directory topology and
  ordinary-file primary-stream bytes, but still does not capture packages,
  drivers, containers, hardware, locale, system libraries, descriptor
  semantics, or anything outside `workspace.root`.
- `plan.json` and the transient resume decision record absolute run, suite, and
  suite-file paths, and `bundle.json` records the absolute suite-file path.
  Writer-lock records and recovery decisions can contain the normalized
  absolute run or workspace-root path and owner PID. Those values can expose a
  local username, drive letter, mount point, or other host layout. Treat raw
  evidence and locks as potentially identifying. Editing records in place invalidates
  their evidence bindings and is not a redaction mechanism.
- Logs may contain data emitted by commands. BenchHandoff does not redact
  secrets.
- Bundles are unsigned local manifests. A party able to rewrite the entire run
  directory can fabricate a self-consistent replacement.
- BenchHandoff has no archive export, signature, transparency log, remote attestation,
  cost accounting, or benchmark comparison engine.

## Claim boundary

The current unreleased v0.5 local checkpoint reports public-privacy PASS,
229 Python passes with 4 Windows capability skips and no failures or errors,
and PASS for Go formatting, module-tidiness verification, module verification,
`go vet`, and unit tests. A trimpath manager build passed locally with
`-buildvcs=false`, required because this worktree is nested inside another
local repository boundary. The Go race test is unavailable in the current
local Windows environment. Real kind and public CI have not yet passed on an
immutable v0.5 commit.
These mutable local observations are not release or takeover evidence.

Recorded v0.4 validation is maintainer-operated and synthetic. It includes the
226-test Ubuntu/Windows matrix, evidence generation, and exact-distribution
smoke in public
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
plus one pinned single-node Kubernetes lifecycle in
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
The runs bind different exact commits; the later commit changes only the
Windows process-family test marker, not the controller implementation.

The final v0.4 release commit remains
`pre-rewrite-commit-retired`, with recorded real-kind run
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
tag CI
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
and GitHub Release record `pre-rewrite-release-retired`. Those facts do not transfer to v0.5.

No claim is made about production use, external users, independent
reproduction, real-workload performance, a general Kubernetes compatibility
range, or compatibility with a specific embodied-simulation stack. A GitHub
tag or Release is a distribution event; it does not establish any of those
external claims.
