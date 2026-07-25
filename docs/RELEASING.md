# Release Process

This is a manual, fail-closed checklist for a future BenchHandoff release. It is
not evidence that a release has occurred, and it does not authorize publication.
CI may build downloadable diagnostic artifacts, but no package-registry
publishing workflow is enabled.

## 1. Resolve release blockers

Stop unless every item is complete:

1. The owner has explicitly selected the license.
2. The pending LICENSING_STATUS.md has been removed, and the exact license
   text and package metadata agree.
3. Ownership, AI-assistance disclosure, fixtures, and third-party provenance
   have been reviewed.
4. The project and package names have been rechecked immediately before
   publication.
5. The version and changelog agree, and the changelog makes no unverified
   release, adoption, performance, or compatibility claim.
6. GitHub private vulnerability reporting is enabled for the public repository;
   a public issue is not the security-reporting fallback.
7. The candidate commit is clean, immutable, and identified by full SHA.

If the license remains pending, do not build a publishable candidate, upload to
TestPyPI, create a public repository, tag a release, or distribute artifacts.

The state validator accepts either the documented pending state or one exact
final state:

```bash
python tools/verify_license_state.py
```

`--require-final` rejects the pending state and is the package/release gate.
The two allowed final choices are `Apache-2.0` and `MIT`. Their candidate bytes
are bound to SPDX `license-list-data` commit
`c4a7237ec8f4654e867546f9f409749300f1bf4c`. The MIT candidate replaces the
template's `<year> <copyright holders>` with `2026 tiramitree`; no other
license-text edits are accepted.

After the owner explicitly chooses one option, keep the canonical candidate
outside the checkout and first run the finalizer without `--apply`:

```bash
source_commit="$(git rev-parse HEAD)"
python tools/finalize_license.py \
  --license Apache-2.0 \
  --license-file /absolute/path/to/canonical-candidate.txt \
  --expected-source-commit "$source_commit"
```

Use `--license MIT` only if that is the owner's explicit choice. Review the
JSON plan, then repeat the same command with `--apply`. The tool requires the
exact clean source commit, writes `LICENSE`, adds PEP 639 metadata, raises the
build backend floor to `setuptools>=77.0.3`, removes the pending notice, and
verifies the resulting state. It does not commit, tag, upload, or publish.
After application, run:

```bash
python tools/verify_license_state.py --require-final
```

## 2. Validate the source commit

Run the full test suite on every operating-system and Python version that the
release will claim. Record the workflow URL, commit SHA, interpreter version,
passes, failures, errors, and skips. A matrix in YAML is only a proposed plan
until those jobs complete.

Also run:

```bash
python tools/verify_license_state.py --require-final
python tools/verify_external_evidence.py
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests tools benchmarks examples
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

## 3. Build once

Use an isolated builder from the exact candidate commit:

```bash
python -m build
python -m twine check --strict dist/*
sha256sum dist/* > dist/SHA256SUMS
```

Inspect the wheel and source archive. Confirm that they contain the selected
license, required documentation, expected package files, and no secrets,
credentials, caches, local paths, test outputs, or unrelated artifacts.

The files in `dist/` are the release candidates. Do not rebuild separately for
TestPyPI, GitHub Releases, or PyPI. All later checks and uploads must use these
same bytes, verified by SHA-256.

## 4. Test the exact wheel outside the checkout

Create a fresh environment and working directory outside the repository.
Install the exact wheel by path, check its metadata version, run CLI help, and
exercise start, resume after the intentional synthetic failure, and verify. Do
not let `PYTHONPATH` point at the source checkout.

Record the wheel SHA, commands, exit codes, and environment. A successful
source-tree test is not a substitute for this installed-wheel test.

## 5. TestPyPI gate

Upload the exact candidate files to TestPyPI, then download that version into a
new directory. Compare each downloaded artifact's SHA-256 to `dist/SHA256SUMS`.
Install the downloaded wheel in another clean environment and repeat the
installed-wheel smoke test.

Stop if the name/version is unavailable, any hash differs, metadata is wrong,
installation resolves unexpected content, or the smoke test fails. Do not
change and reuse the same version; repair the source, increment the candidate
version as appropriate, rebuild, and restart the checklist.

## 6. Public release

After all gates pass:

1. create the final tag on the exact tested commit;
2. upload the same source archive and wheel bytes to PyPI;
3. verify the PyPI downloads against the recorded SHA-256 values;
4. create the GitHub Release for the same tag;
5. attach the same artifacts, `SHA256SUMS`, bounded benchmark records, and
   release notes; and
6. link the completed public CI runs and state all observed skips and
   limitations.

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

The owner must make any irreversible publication, license, yanking, or public
security-disclosure decision.
