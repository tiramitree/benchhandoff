# Changelog

All notable versioned changes are documented in this file. A GitHub Release is
not a PyPI publication, supported-production claim, or external-adoption event.

## 0.5.0 - 2026-07-30

Version 0.5.0 is a GitHub-only early release only when its matching annotated
tag and GitHub Release exist. A source commit or changelog entry alone is not a
release. The controller remains source-only; no controller image,
package-registry publication, production support, independent review, external
use, or adoption is asserted.

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
- Two registered clean-commit, single-node kind takeover gates. The first
  deletes the Lease holder while a synthetic `start` Job is live. The second
  temporarily removes only business RBAC, lets `resume` become terminal while
  its result is still pending in `AgentRun` status, then deletes the holder.
  Each binds the manager Pod UIDs to a stable Lease resource version, admits
  only the pre-existing passive Pod at the next transition, and requires
  unchanged measured Job/Pod identities and one-object cardinalities.
- A bounded, privacy-gated `takeover-evidence.json` plus one-entry
  `SHA256SUMS`. The directory must contain exactly those regular files, and
  official upload is restricted to a successful trusted push or manual
  dispatch rather than a pull-request execution.
- Takeover evidence schema 2, adding the terminal Job/Pod result, pending
  `AgentRun` binding, business-RBAC shape digests, post-restore Lease/passive
  continuity, and final per-action cardinalities. Earlier schema 1 candidate
  artifacts remain historical and do not satisfy the current v0.5 gate.

### Fixed

- Windows recovery now uses absolute extended-length paths for deterministic
  quarantine destinations that exceed the legacy `MAX_PATH` boundary. The
  no-replace move, post-move identity check, crash replay, and test cleanup are
  covered without changing the existing same-filesystem or no-overwrite
  contract.
- The Windows hard-exit process-family test now tolerates the bounded interval
  in which its child has created but not yet completed the small JSON fixture.

### Current validation boundary

This source tree intentionally omits validation identifiers from superseded
history. The checked-in CI and real-kind workflows define the registered
procedures. The release claim applies only when the current annotated tag peels
to a commit that completed both public CI and real-kind gates.

Version 0.5 is a fixed active/passive takeover experiment with one kind node
and continuously available API-server storage. It does not establish strict
fencing, network-partition safety, arbitrary Pod recovery, multi-node or
multi-cluster availability, exactly-once execution, production high
availability, external adoption, independent review, or recruiting interest.

## 0.4.0 - 2026-07-29

Version 0.4 is a GitHub-only early release. Its current annotated tag,
workflow results, and attached assets must be evaluated from the rewritten
public tag and Release; superseded history identifiers are not evidence for
the current source tree. Any recorded checks are maintainer-operated and
synthetic, not production, independent, performance, or external-adoption
evidence.

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

- The registered kind workflow runs Go formatting, module verification,
  `go vet`, race tests, and the bounded real-API lifecycle before publishing
  any evidence.
- The ordinary workflow runs the declared operating-system/Python matrix,
  privacy and license gates, synthetic evidence verification, and
  exact-distribution smoke.
- Workflow outcomes belong only to their exact source revision. A successful
  result from superseded history is not carried forward to a rewritten tag.

See
[`AGENTRUN_CONTROLLER.md`](docs/AGENTRUN_CONTROLLER.md)
for the protocol, API example, reproduction command, and complete claim
boundary.

## 0.3.0 - 2026-07-28

Version 0.3 is a GitHub-only early release. Its maintainer-operated synthetic
validation and any tag or GitHub Release do not assert external adoption,
performance, production reliability, or a support commitment.

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

- The registered workflow verifies the operating-system/Python matrix,
  canonical synthetic evidence, and build-once exact distribution for the
  exact checked-out revision.
- This is maintainer-operated synthetic validation, not production,
  independent, external-use, or adoption evidence. The current annotated tag
  and Release, not identifiers copied from superseded history, are the
  distribution record.

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
  smoke testing behind the explicit license gate.
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
- Maintainer-run Windows compatibility preflight across CPython 3.11 through 3.14,
  including one-wheel installation and fail -> bound resume -> verify smoke
  coverage. This is not online CI or a final licensed distribution.
- A cooperative local cross-process writer lock around every `start` and
  `resume` mutation, exact-record release checks, fail-closed orphan behavior,
  six focused lock regressions, and a two-process synthetic contention
  benchmark. This is not a remote lease, distributed coordinator,
  network-filesystem proof, or hostile-writer boundary.
- A commit-bound maintainer-run Windows writer-lock matrix across CPython 3.11.15,
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
