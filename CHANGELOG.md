# Changelog

All notable versioned changes are documented in this file. A GitHub Release is
not a PyPI publication, supported-production claim, or external-adoption event.

## 0.5.0 - Unreleased

This section describes an unreleased source candidate. No v0.5 tag, GitHub
Release, real-kind result, public-CI result, independent review, external use,
or adoption is asserted.

### Added

- A fixed two-replica reference manager deployment coordinated by the
  namespaced Lease
  `benchhandoff-system/agentrun-controller.benchhandoff.dev`.
- Fixed client-go leader-election settings: 15-second Lease duration,
  10-second renew deadline, 2-second retry period, Lease resource lock, and
  `LeaderElectionReleaseOnCancel=false`.
- A precreated exact Lease plus a namespaced, `resourceNames`-restricted Role
  that grants only `get` and `update`. It grants no create, list, watch, patch,
  delete, other-Lease update, or cross-namespace Lease access.
- Uncached post-create observation that requires one deterministic action Job,
  one matching server UID, the complete audited template, and a non-ambiguous
  Pod set after either create success or `AlreadyExists`.
- Status-conflict handling that discards the stale candidate and requeues a
  complete fresh reconcile instead of retrying a stale status update.
- Two registered clean-commit, single-node kind takeover gates. Each cordons
  the node while a synthetic `start` or `resume` Job is live, binds one stable
  Lease version to both manager Pod UIDs, and UID-precondition deletes the
  holder. Only the pre-existing passive Pod may acquire exactly the next
  transition; the gate then uncordons the node and requires unchanged measured
  Job/Pod identities and one-object cardinalities.
- A bounded, privacy-gated `takeover-evidence.json` plus one-entry
  `SHA256SUMS`. The directory must contain exactly those regular files, and
  official upload is restricted to a successful trusted push or manual
  dispatch rather than a pull-request execution.

### Current validation boundary

- Local public-privacy verification: PASS.
- Local Python suite: 229 passed, 4 Windows capability skips, 0 failures, and
  0 errors.
- Local Go formatting, module-tidiness verification, module verification,
  `go vet`, and unit tests: PASS. The local trimpath manager build passed with
  `-buildvcs=false` because the worktree is nested inside a separate local
  repository boundary.
- The Go race test is unavailable in the current local Windows environment.
- The real kind takeover gate and public CI have not yet run for this
  candidate. No v0.5 evidence artifact or release is claimed.

The candidate is a fixed active/passive takeover experiment with one kind node
and continuously available API-server storage. It does not establish strict
fencing, network-partition safety, arbitrary Pod recovery, multi-node or
multi-cluster availability, exactly-once execution, production high
availability, external adoption, independent review, or recruiting interest.

## 0.4.0 - 2026-07-29

The final v0.4 release commit is
`pre-rewrite-commit-retired`. Its recorded public real-kind run
is
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
its recorded tag CI is
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
and its GitHub Release record is `pre-rewrite-release-retired`.

AgentRun code-bearing commit
`pre-rewrite-commit-retired` completed the public pinned
single-node Kubernetes
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
Commit `pre-rewrite-commit-retired` completed all ten Python,
privacy, evidence, and exact-distribution jobs in public
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
These are maintainer-operated synthetic results, not production, independent,
performance, or external-adoption evidence.

### Added

- A namespaced `control.benchhandoff.dev/v1alpha1` `AgentRun` CRD with an
  immutable execution spec and a write-once resume approval.
- A Go controller that binds deterministic start/resume/verify Jobs to the
  `AgentRun` UID, canonical execution-spec SHA-256, exact owner reference,
  audited template, live Job UID, and one owned Pod.
- A path-free, canonical 1 KiB termination-message protocol between the Python
  runner bridge and the manager. The manager does not read Pod logs or PVC
  bytes.
- `benchhandoff.controller_step`, which requires a version 3 suite, checks its
  raw byte digest before parsing and again at engine entry, performs one bound
  action, and emits only registered result fields and error categories.
- A distinct verify Job after either start or resume completion. `Succeeded`
  requires a fresh verification result with the same run and bundle identities.
- Non-root runner Jobs with a read-only root filesystem, disabled service
  account token and service links, dropped capabilities, runtime-default
  seccomp, and no privilege escalation.
- Pinned, disposable kind E2E coverage for deliberate failure, exact approval,
  bound resume, fresh verify, manager restart/adoption, a declared suite-digest
  mismatch, wrong approval, duplicate Pod rejection, race tests, and bounded
  cleanup.

### Fail-closed boundaries

- Execution-affecting Job drift, missing or duplicate Jobs, a replaced Job UID,
  a foreign or duplicate matching Pod, malformed termination data, mismatched
  run/spec/action identities, and failed fresh verification move the resource
  to `Blocked`.
- A digest-pinned runner image and suite SHA-256 bind bytes. They do not prove
  provenance, safety, architecture compatibility, workload validity, or the
  absence of external side effects.
- Kubernetes rules using `oldSelf` are skipped on CREATE. A prefilled approval
  can pass admission, but the controller blocks it as `PreseededApproval`
  before creating any Job. Wrong first approval on UPDATE is rejected, and an
  accepted approval cannot later be changed or removed.
- Manager restart/adoption is tested for one replica in one single-node kind
  cluster. There is no high-availability, leader-election, network-partition,
  multi-cluster, storage-fencing, or distributed-lease evidence.
- Resume remains at-least-once. The controller is not a sandbox, workload
  authorization service, general scheduler, exactly-once system, production
  operator, or supported deployment.
- The checked-in Kustomize deployment contains an E2E image placeholder. No
  controller image, Helm chart, production installer, or compatibility matrix
  is published.

### Validation notes

- The kind gate used Go 1.26.5, kind v0.32.0, Kubernetes v1.36.1, Kubernetes Go
  modules v0.36.0, and digest-pinned runner and registry images.
- The kind workflow passed Go formatting, module verification, `go vet`, and
  `go test -race ./...` before exercising the real API server and Jobs.
- Every Ubuntu 24.04 and Windows Server 2025 job across CPython 3.11 through
  3.14 ran 226 tests without failures, errors, or skips at `pre-rewrite-commit-retired...`.
- The first public Python matrix on `pre-rewrite-commit-retired...` exposed a test-helper race in
  marker publication on Windows/Python 3.14. The helper now writes a candidate
  and atomically replaces the visible marker; ten repeated local Windows runs
  and the full public matrix passed after the repair.

See
[`AGENTRUN_CONTROLLER.md`](docs/AGENTRUN_CONTROLLER.md)
for the protocol, API example, reproduction command, and complete claim
boundary.

## 0.3.0 - 2026-07-28

Code-bearing commit `pre-rewrite-commit-retired`
completed all ten jobs in public main-branch CI
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
That maintainer-operated synthetic validation and any tag or GitHub Release do
not assert external adoption, performance, production reliability, or a support
commitment.

### Added

- Strict suite/evidence schema version 3 with one dedicated `workspace.root`, a
  canonical reviewed manifest outside that root, and version 1/version 2 read
  and execution compatibility.
- `snapshot-workspace`, which publishes a start-absent manifest candidate for
  human review, and `inspect-workspace`, which validates a version 3 suite
  without launching a task.
- Bounded, double-scanned workspace observations bind directory topology and
  ordinary-file primary-stream bytes under `workspace.root`. Version 3 rejects
  links, reparse points, hard-linked files, cross-device entries, unsupported
  entry types, path aliases, and manifest or topology drift.
- Derivable `workspace_before`, terminal `workspace_after`, and, after
  quarantine recovery, `workspace_recovered` attempt summaries. The final
  workspace binding is copied into `bundle.json` and freshly checked by
  `verify`.
- A second cooperative writer lock keyed to the workspace root, preventing two
  BenchHandoff mutation entrypoints that use different run directories from
  concurrently operating on the same reviewed workspace.
- Atomic no-replace quarantine moves on Windows, Linux, and macOS. Unsupported
  platforms or unavailable native no-replace primitives fail closed.

### Boundaries

- Workspace integrity is checked at discrete preflight, launch, post-exit,
  recovery, bundle, and verification points. It is not continuous monitoring,
  a sandbox, or a hostile-writer boundary.
- Only bounded directory topology and ordinary-file primary-stream bytes under
  `workspace.root` are observed. Writes elsewhere, network activity, and other
  side effects are neither prevented nor recorded. A same-device bind mount may
  not be detected by device-id checks.
- The workspace identity covers directory topology and ordinary-file
  primary-stream bytes, not mode, owner, timestamps, ACLs, extended attributes,
  NTFS alternate data streams, sparse-file layout, or unlisted metadata.
- A snapshot manifest discloses relative paths, entry kinds, file sizes, and
  content hashes. Raw run evidence and writer-lock records can still disclose
  absolute local paths.
- A failed snapshot publication or re-verification retains the candidate path
  for review; it does not silently remove bytes whose ownership may be
  ambiguous.
- A hard runner crash may leave a `running` attempt without a terminal
  workspace observation. Recovery records the then-current tree before
  quarantine; it does not reconstruct or claim the exact crash-time tree.
- Schema versions 1 and 2 retain their original evidence shapes and execution
  behavior. Version 3 is opt-in and does not retroactively strengthen old runs.

See
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](docs/CLOSED_WORLD_WORKSPACE_INTEGRITY.md)
for the detailed protocol and limits.

## 0.2.0 - 2026-07-28

### Added

- Strict suite/evidence schema version 2 while retaining version 1 read and
  execution compatibility.
- Byte-verified execution-context descriptor files that must also be declared
  seed inputs.
- Resolved executable content identity plus a hashed normalized-path identity,
  with absolute `argv[0]` rejected for version 2.
- A non-inheriting version 2 launch environment with only derived runner
  variables and hashed-and-bound Windows `SystemRoot`.
- Windows Job Object lifecycle control using suspended creation, assignment
  before resume, kill-on-close, active-member queries, and empty-scope gates.
- Cooperative Linux session/process-group lifecycle control with TERM/KILL,
  bounded empty confirmation, and resume-time residual-group refusal.
- Process-spawning synthetic child-to-grandchild tests for explicit cleanup,
  leader exit, state-write failure, and hard runner exit on both platform
  branches.
- Linux PID liveness distinguishes terminal zombies from active processes and
  treats unreadable or malformed `/proc` identity as unknown.
- A public synthetic version 2 example and a protocol/claim-boundary note.

### Boundaries

- The descriptor is opaque and is not a VM/container snapshot implementation,
  package or driver inventory, or remote attestation.
- The Windows Job and Linux process group are lifecycle controls, not a
  sandbox. Linux descendants can deliberately leave the group.
- Linux version 2 preserves the declared portable `argv[0]` while omitting
  `PATH`; programs cannot assume that `argv[0]`-based self-location, including
  CPython `sys.executable`, can reconstruct the bound executable.
- These maintainer-authored synthetic tests are not production reliability,
  external use, independent reproduction, or third-party review.

### Validation

- Version 2 code-bearing commit
  `pre-rewrite-commit-retired` completed all ten jobs in public
  main-branch run
  [pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
  Eight Ubuntu 24.04 and Windows Server 2025 jobs across CPython 3.11 through
  3.14 each ran 186 tests without failures, errors, or skips. The two dependent
  jobs verified the canonical synthetic evidence and build-once exact
  distribution. This is maintainer-operated synthetic validation, not
  production, independent, external-use, or adoption evidence.
- Release commit `pre-rewrite-commit-retired`
  repeated all ten gates in public
  [run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions).
  The annotated `v0.2.0` tag and GitHub Release bind the exact wheel, sdist,
  checksum file, and five synthetic-evidence files from that run.

## 0.1.0 - 2026-07-26

### Added

- Local CLI commands for starting, resuming, and verifying a flat sequential
  suite.
- Apache License 2.0, bound to the canonical SPDX license-list-data text and
  matching PEP 639 package metadata.
- Fail-closed process, path, retry, quarantine, and evidence checks.
- Versioned plan, state, event-chain, and final-bundle records.
- Synthetic recovery benchmarks with raw result records and explicit claim
  boundaries.
- Local unit tests, architecture notes, evidence-format documentation, a basic
  example, and public-project governance documents.
- A three-task recovery example that exercises exit `20`, partial-output
  quarantine, suffix-only resume, and final verification.
- A top-level `--version` command plus source/package metadata consistency tests.
- A public eight-job Python/operating-system matrix, commit-pinned Actions,
  canonical machine-readable benchmark artifacts, and build-once exact-wheel
  smoke testing behind the explicit license gate. The first complete green
  run is `pre-rewrite-run-retired` at source `pre-rewrite-commit-retired...`.
- A single cross-platform synthetic-reproduction entrypoint that refuses dirty
  source, overwrite, linked or nonempty parents, and in-repository output, then
  writes two raw records, a bounded summary, a verified SHA-256 manifest, and a
  completion record.
- A read-only mode on the same entrypoint that verifies exact topology and file
  bounds, regular non-linked inputs, strict canonical JSON, all hash and record
  bindings, deterministic benchmark invariants, and an optional expected
  source commit.
- A mutation-free `inspect` command that emits a deterministic resume decision
  SHA-256 over current evidence, relevant inputs, completed outputs, and
  partial-output/quarantine observations.
- Optional evidence-bound resume that rejects a stale decision before the first
  transition, with a focused synthetic drift/rejection/refresh invariant.
- A canonical zero-baseline external-evidence ledger, strict structural and
  count validator, relationship/consent-aware Issue forms, and documented
  review, deduplication, and retraction rules.
- Local Windows compatibility preflight across CPython 3.11 through 3.14,
  including one-wheel installation and fail -> bound resume -> verify smoke
  coverage. This is not online CI or a final licensed distribution.
- A cooperative local cross-process writer lock around every `start` and
  `resume` mutation, exact-record release checks, fail-closed orphan behavior,
  six focused lock regressions, and a two-process synthetic contention
  benchmark. This is not a remote lease, distributed coordinator,
  network-filesystem proof, or hostile-writer boundary.
- A commit-bound local Windows writer-lock matrix across CPython 3.11.15,
  3.12.13, 3.13.14, and 3.14.6. Each runtime completed 129 tests with 126
  passes and the same 3 permission skips. This is not Linux, online CI,
  public-release, production, independent, or adoption evidence.

### Security

- Direct argument-array execution with `shell=False`.
- Rejection of unsupported path forms, links, non-regular declared files, and
  ambiguous child-process state.

### Not included in this GitHub-only release

- TestPyPI or PyPI publication. A future package-registry release requires its
  own explicit authorization and the registry gates in `docs/RELEASING.md`.
- A bounded, cross-platform, reparse-safe diagnostic export design; an initial
  local implementation was withheld after a separate Codex-assisted local
  static review found unresolved Windows path-race and resource-budget risks.

The GitHub Release attaches the exact CI-built wheel and sdist, their
`SHA256SUMS`, and the verified synthetic evidence records. It does not change
the zero external-evidence baseline.
