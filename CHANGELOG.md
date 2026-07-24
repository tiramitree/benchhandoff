# Changelog

All notable changes intended for a future public release will be documented in
this file.

The project is not yet published. An entry under `Unreleased` is not a release,
and no external adoption is claimed.

## Unreleased

### Added

- Local CLI commands for starting, resuming, and verifying a flat sequential
  suite.
- Fail-closed process, path, retry, quarantine, and evidence checks.
- Versioned plan, state, event-chain, and final-bundle records.
- Synthetic recovery benchmarks with raw result records and explicit claim
  boundaries.
- Local unit tests, architecture notes, evidence-format documentation, a basic
  example, and public-project governance documents.
- A three-task recovery example that exercises exit `20`, partial-output
  quarantine, suffix-only resume, and final verification.
- A top-level `--version` command plus source/package metadata consistency tests.
- A proposed eight-job Python/operating-system matrix, commit-pinned Actions,
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

### Security

- Direct argument-array execution with `shell=False`.
- Rejection of unsupported path forms, links, non-regular declared files, and
  ambiguous child-process state.

### Pending before any release

- Explicit owner selection of an open-source license and matching package
  metadata.
- Public CI evidence across the claimed operating-system and Python matrix.
- Exact-artifact build, TestPyPI verification, and release review described in
  `docs/RELEASING.md`.
- A bounded, cross-platform, reparse-safe diagnostic export design; an initial
  local implementation was withheld after independent review found unresolved
  Windows path-race and resource-budget risks.

No versioned release entry should be added until the exact public artifacts and
tag exist.
