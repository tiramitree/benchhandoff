# Release Process

This is a manual, fail-closed checklist for BenchHandoff releases. CI builds
downloadable artifacts, but no package-registry publishing workflow is
enabled.

BenchHandoff version lines are GitHub-only early releases under Apache-2.0.
Each tag, GitHub Release, and attached asset set binds to one exact
public-CI-tested commit. Every release must repeat the same exact-commit
gates. There is no TestPyPI or PyPI publication, supported production line, or
external-adoption claim.

The `v0.3.0` annotated tag peels to release commit
`pre-rewrite-commit-retired`. Exact tag
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions)
completed all ten jobs before the three CI-built distribution assets were
attached. This is a historical release record, not a substitute for repeating
the gates on a later candidate. Neither those gates nor a tag or Release
establishes adoption, production compatibility, or performance.

The current completed `v0.4.0` release commit is
`pre-rewrite-commit-retired`. Its recorded real-kind run is
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
its recorded tag CI is
[pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions),
and its GitHub Release record is `pre-rewrite-release-retired`. These historical facts do not
substitute for any v0.5 candidate gate.

## 1. Resolve release blockers

Stop unless every item is complete:

1. `LICENSE`, package metadata, source headers where present, and release notes
   all retain the already-selected Apache-2.0 license.
2. `python tools/verify_license_state.py --require-final` passes. A pending
   licensing notice or any alternative-license metadata is a blocker.
3. Ownership, AI-assistance disclosure, fixtures, and third-party provenance
   have been reviewed.
4. The project and package names have been rechecked immediately before
   publication.
5. The version and changelog agree, and the changelog makes no unverified
   release, adoption, performance, or compatibility claim.
6. GitHub private vulnerability reporting is enabled for the public repository;
   a public issue is not the security-reporting fallback.
7. The candidate commit is clean, immutable, and identified by full SHA.

The repository license is no longer a pending choice. Verify the final
Apache-2.0 state directly:

```bash
python tools/verify_license_state.py --require-final
```

Do not rerun the historical license-finalization workflow or present MIT as a
routine release option. Relicensing would be a separate legal and provenance
decision requiring explicit owner review; it is outside this release checklist.

## 2. Validate the source commit

Run the full test suite on every operating-system and Python version that the
release will claim. Record the workflow URL, commit SHA, interpreter version,
passes, failures, errors, and skips. A matrix in YAML is only a proposed plan
until those jobs complete.

For a version 3 candidate, additionally stop unless:

- a clean synthetic workspace was snapshotted to a start-absent candidate
  outside `workspace.root`, reviewed, and bound exactly in `suite.toml`;
- `inspect-workspace`, start, and verify were run with the run directory outside
  the entire suite tree;
- workspace drift and failed/quarantined recovery paths were exercised without
  weakening the registered boundary;
- reviewers checked manifest relative paths, kinds, sizes, and hashes and raw
  evidence/lock absolute paths for sensitive metadata; and
- the release notes state that observations are discrete, cover only
  `workspace.root`, do not detect every same-device bind mount, and are not a
  sandbox or hostile-writer boundary.
- the release notes limit identity to directory topology and ordinary-file
  primary-stream bytes, excluding permissions, ownership, timestamps, ACLs,
  xattrs, NTFS alternate streams, sparse layout, and unlisted metadata.

If snapshot publication or re-verification fails, preserve the candidate for
review. Do not silently delete it or reuse its path. The no-replace quarantine
claim is limited to Windows, Linux, and macOS with the required native
primitive; missing support must fail closed. See
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](CLOSED_WORLD_WORKSPACE_INTEGRITY.md).

For a version 0.4 or later `AgentRun` candidate, additionally stop unless:

- Go formatting, `go mod tidy -diff`, module verification, `go vet`, and
  `go test -race ./...` pass on the exact candidate;
- the ordinary CI and pinned real-API kind workflows both succeed on that same
  exact candidate, with the kind gate covering failure, approval, resume, fresh
  verify, manager restart/adoption, a declared suite-digest mismatch, wrong
  approval, duplicate Pod rejection, runner security-context checks, and
  bounded cleanup;
- the release notes identify the exact kind, Kubernetes, kubectl, Go, and
  Kubernetes-module versions used by that one observed gate;
- the release notes state that the decision digest is not a credential, that
  CREATE-time preseeded approval is controller-blocked rather than
  admission-rejected, and that status and termination messages are not signed
  or remotely attested;
- the release notes preserve the no-sandbox, at-least-once, single-node,
  no-general-compatibility, and no-production boundaries;
- for a v0.5 candidate, the exact Lease is precreated and its
  `resourceNames`-restricted Role permits only `get` and `update`;
- for a v0.5 candidate, each registered gate starts and ends on the same clean
  commit, binds one stable Lease version to both manager Pod UIDs, cordons the
  node, UID-precondition deletes the holder, requires the pre-existing passive
  to acquire exactly the next transition, restores scheduling and two Ready
  replicas, and preserves measured one-object start/resume Job and Pod
  identities;
- for a v0.5 candidate, the privacy-gated takeover JSON and its one-entry
  checksum are the only two regular artifact files, pass the repository privacy
  scanner, and are uploaded only by a successful trusted push or dispatch; the
  release notes explicitly deny strict fencing, network-partition safety,
  arbitrary Pod recovery, multi-node/multi-cluster availability, exactly-once,
  and production HA; and
- the source and distribution privacy gates pass without publishing raw
  manifests, suite paths, PVC contents, Pod logs, or run evidence.

The checked-in manager image is a test placeholder. Unless an exact public
image is separately built, privacy-gated, digest-bound, and explicitly
released, the controller remains source-only. A Python wheel or source archive
does not contain or publish a controller image.

Also run:

```bash
python tools/verify_license_state.py --require-final
python tools/verify_external_evidence.py
python tools/verify_public_privacy.py
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests tools benchmarks \
  examples/recovery_pipeline examples/context_bound
evidence_parent="$(mktemp -d)"
evidence_root="$evidence_parent/package"
expected_commit="$(git rev-parse HEAD)"
PYTHONPATH=src python benchmarks/synthetic/reproduce.py \
  --output-dir "$evidence_root"
PYTHONPATH=src python benchmarks/synthetic/reproduce.py \
  --verify-dir "$evidence_root" \
  --expected-commit "$expected_commit"
```

The temporary parent must be outside the checkout and empty, and the package
path must start absent. Retain the exact five-file verified package as build
evidence. The verifier checks internal topology, bounds, hashes, records, and
commit binding; it does not authenticate where a downloaded package came from.
Describe these as synthetic child-work results, not wall-clock or production
performance.

## 3. Local package preflight

A local isolated build may be used to catch packaging faults before publication:

```bash
python -m build
python -m twine check --strict dist/*
sha256sum dist/* > dist/SHA256SUMS
```

Inspect these local wheel and source-archive bytes for the selected license,
required documentation, expected package files, secrets, credentials, caches,
local paths, test outputs, and unrelated artifacts.

Local build output is preflight material only and must never be uploaded. The
`package` job in the public tag CI is the sole release builder. It builds once,
runs the privacy and installed-wheel gates, records SHA-256, and uploads the
exact release candidates. GitHub Release uploads must use only those downloaded
CI bytes.

## 4. Test the exact preflight wheel outside the checkout

Create a fresh environment and working directory outside the repository.
Install the exact wheel by path, check its metadata version, run CLI help, and
exercise start, resume after the intentional synthetic failure, and verify. Do
not let `PYTHONPATH` point at the source checkout.

Record the wheel SHA, commands, exit codes, and environment. A successful
source-tree test is not a substitute for this installed-wheel test.

## 5. GitHub Release gate

For a GitHub-only release:

1. require complete green public ordinary CI and, for version 0.4 or later,
   AgentRun real-kind E2E runs on the exact candidate commit;
2. create and push an annotated final tag on that exact commit;
3. require the tag ref to peel to the candidate and wait for complete green
   ordinary CI and AgentRun real-kind E2E runs whose head SHA is the candidate;
   if a path-filtered tag event does not create the AgentRun run, manually
   dispatch that workflow against the exact tag ref and verify the same head
   SHA;
4. download the distribution and evidence artifacts only from the successful
   tag CI;
5. compare the downloaded files to the workflow's recorded SHA-256 values;
6. create the GitHub Release for the already-pushed annotated tag;
7. attach the same wheel, sdist, and `SHA256SUMS`; attach synthetic evidence
   only when the release scope explicitly calls for those exact privacy-gated
   CI bytes, and never attach raw run directories or logs; and
8. download every attached release asset again and verify exact names, sizes,
   hashes, metadata, and the installed-wheel smoke path.

State all observed skips and limitations. Do not rebuild release assets after
CI. If any release download differs, publish no replacement bytes under the
same version.

## 6. Optional package-registry gate

Package-registry publication is a separate action and is not implied by a
GitHub Release. It requires explicit owner authorization. Before any PyPI
upload, use TestPyPI:

Upload the exact candidate files to TestPyPI, then download that version into a
new directory. Compare each downloaded artifact's SHA-256 to `dist/SHA256SUMS`.
Install the downloaded wheel in another clean environment and repeat the
installed-wheel smoke test.

Stop if the name/version is unavailable, any hash differs, metadata is wrong,
installation resolves unexpected content, or the smoke test fails. Do not
change and reuse the same version; repair the source, increment the candidate
version as appropriate, rebuild, and restart the checklist. Only after that
gate may the same bytes be uploaded to PyPI and downloaded again for hash and
installed-wheel verification.

Do not describe TestPyPI installation, repository views, downloads, issues, or
self-authored examples as external adoption. Publication does not change the
zero external-evidence baseline; only a later human-reviewed ledger record can.

## 7. Failure and rollback

Published package bytes and version history are evidence. Never overwrite or
silently replace them.

- Before PyPI upload: stop and discard the candidate without publishing.
- After an incorrect or unsafe PyPI upload: preserve evidence, consider yanking
  the affected release, publish a clear notice, fix on a new version, and repeat
  the entire checklist.
- After a GitHub Release problem: do not substitute different bytes under the
  same version. Mark the affected release clearly and publish corrected
  artifacts under a new version.
- For a security issue: follow `SECURITY.md`, avoid public exploit details, and
  document the supported-version decision after human review.

The owner must make any new package-registry, yanking, license, or public
security-disclosure decision.
