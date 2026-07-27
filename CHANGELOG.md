# Changelog

All notable versioned changes are documented in this file. A GitHub Release is
not a PyPI publication, supported-production claim, or external-adoption event.

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
