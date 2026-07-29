# Reproducing BenchHandoff Evidence

This guide separates three different claims:

1. **Source validation**: tests pass for one recorded source commit and
   environment.
2. **Synthetic result reproduction**: the fixed fixtures produce the asserted
   child-work counts and final hash relationship.
3. **Independent reproduction**: an outside person publishes enough evidence to
   inspect a reproduction attempt.

The first two can be performed by the maintainer. They do not establish the
third, production use, or external adoption.

## Record the source and environment

Use a clean checkout of an exact commit and record:

```bash
git rev-parse HEAD
git status --short
python --version
python -c "import platform,sys; print(platform.system() or "Unknown"); print(sys.implementation.name)"
```

Record whether Python came from a virtual environment, the operating system,
and every skipped check. Do not publish a local checkout path, username, token,
environment dump, or other identifying value.

## Run source validation

From the repository root:

```bash
python tools/verify_external_evidence.py
python tools/verify_public_privacy.py
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests tools benchmarks examples
```

On PowerShell, set `$env:PYTHONPATH = "src"` first. Report passes, failures,
errors, and skips separately. Do not convert a skipped platform capability into
a pass.

The version 2 lifecycle tests create real child and grandchild processes. On
Windows they exercise Job assignment-before-resume and kill-on-close after a
hard runner exit. On Ubuntu they exercise cooperative process-group
termination and residual-group refusal after a hard runner exit. A green result
supports only those synthetic operating-system boundaries; it is not a sandbox,
production reliability rate, or external reproduction.

## Validate the unreleased v0.5 controller candidate

The current local Windows checkpoint reports public-privacy PASS, 229 Python
passes with 4 Windows capability skips and no failures or errors, plus PASS for
Go formatting, module-tidiness verification, module verification, `go vet`,
and unit tests. A trimpath manager build passed locally with
`-buildvcs=false`; this exception was needed because the worktree is nested
inside a separate local repository boundary. A clean standalone checkout
should use the ordinary build command below. The local Windows environment
cannot run the Go race test. The v0.5 real-kind gate and public CI have not yet
run, so no candidate takeover artifact or public validation is claimed.

Run the Go source checks from a supported environment:

```bash
gofmt -l controller
cd controller
go mod tidy -diff
go mod verify
go vet -mod=readonly ./...
go test -mod=readonly ./...
go test -mod=readonly -race ./...
go build -mod=readonly ./cmd/manager
cd ..
```

The registered real-API gate is:

```bash
bash controller/test/e2e/run_kind.sh
```

It requires Linux, Bash, Docker, outbound access to pinned public images and
downloads, and enough resources for one disposable kind node, one registry,
and two manager Pods. The candidate gate deletes the observed Lease-holder
manager while synthetic `start` and `resume` Jobs are separately paused. It
requires a clean source tree at the beginning and end, binds a stable Lease
version to both manager Pod UIDs, cordons the node, and uses a Pod-UID
precondition for deletion. Only the previously observed non-holder Pod may
acquire exactly the next transition. The gate then uncordons the node, restores
two Ready replicas, and requires equal measured before/after Job and Pod
identities with one-object cardinalities.

On success, the script writes `takeover-evidence.json` and `SHA256SUMS` only
below a new bounded temporary evidence directory. It refuses an existing path,
requires exactly two regular files, checks their digest relationship, and
applies the repository privacy scanner to both. CI supplies a new
`E2E_EVIDENCE_DIR` outside the scratch root; it uploads the exact two files
only after a successful trusted push or manual dispatch. Pull-request runs do
not publish an official artifact.

When `E2E_EVIDENCE_DIR` is omitted locally, the default is
`$SCRATCH_ROOT/evidence`, which successful scratch cleanup deletes. To retain a
local record, provide a new, non-existing path under `RUNNER_TEMP`; never point
it at a durable or pre-existing directory. Do not publish the scratch tree, raw
objects, Pod logs, private manifests, or PVC contents. This fixed gate assumes
the API server and its storage remain available and does not prove strict
fencing, network-partition safety, arbitrary Pod recovery, multi-node or
multi-cluster availability, exactly-once effects, or production HA.

## Validate a version 3 workspace

Use a fresh copy of the synthetic version 3 suite. Keep the run directory
outside the complete suite tree. Snapshot the clean workspace to a new path
outside `workspace.root`, review the manifest, bind its digest and size in
`suite.toml`, then run:

```bash
PYTHONPATH=src python -m benchhandoff inspect-workspace path/to/suite.toml
PYTHONPATH=src python -m benchhandoff start path/to/suite.toml \
  --run-dir /separate/path/to/run
PYTHONPATH=src python -m benchhandoff verify /separate/path/to/run
```

Repeat `verify` after adding or changing one workspace entry and require a
failure; do not cite the deliberately drifted tree as completed evidence.
These commands exercise discrete bounded topology-and-primary-stream
observations only. They do not prove continuous monitoring, sandboxing,
hostile-writer resistance, detection of a same-device bind mount, or control
of writes outside `workspace.root`.
The comparison binds directory topology and ordinary-file primary-stream bytes,
not mode, owner, timestamps, ACLs, extended attributes, NTFS alternate data
streams, sparse-file layout, or other metadata.

Review the snapshot separately: relative paths, kinds, sizes, and hashes can be
sensitive metadata, while raw run evidence and lock records may contain
absolute paths. A failed snapshot can retain its candidate and requires manual
review before cleanup or retry. Versions 1 and 2 remain valid under their
original, narrower file boundaries. See
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](CLOSED_WORLD_WORKSPACE_INTEGRITY.md).

## Reproduce the synthetic records

Use the single package entrypoint from a clean exact checkout. It refuses a
dirty source tree, an existing output path, and any output inside the checkout.
On Linux:

```bash
evidence_parent="$(mktemp -d)"
expected_commit="$(git rev-parse HEAD)"
PYTHONPATH=src python benchmarks/synthetic/reproduce.py \
  --output-dir "$evidence_parent/package"
PYTHONPATH=src python benchmarks/synthetic/reproduce.py \
  --verify-dir "$evidence_parent/package" \
  --expected-commit "$expected_commit"
```

On PowerShell:

```powershell
$evidenceParent = New-Item -ItemType Directory -Path (
  Join-Path ([IO.Path]::GetTempPath()) ("benchhandoff-" + [guid]::NewGuid())
)
$expectedCommit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "unable to identify the source commit" }
$evidencePackage = Join-Path $evidenceParent.FullName "package"
$env:PYTHONPATH = "src"
python benchmarks\synthetic\reproduce.py --output-dir $evidencePackage
if ($LASTEXITCODE -ne 0) { throw "package generation failed" }
python benchmarks\synthetic\reproduce.py `
  --verify-dir $evidencePackage `
  --expected-commit $expectedCommit
```

The new package contains:

- `focused-recovery.json`;
- `pipeline-comparison.json`;
- `summary.json`, which binds the source commit, bounded environment, record
  hashes, and asserted synthetic claims; and
- `SHA256SUMS.txt`, which hashes all three JSON records; and
- `PACKAGE_COMPLETE.json`, written last, which binds the manifest identity and
  required file list.

The entrypoint requires a newly created, empty, caller-private parent directory;
it rejects symlink or reparse-point parents and rechecks the parent identity
before writing. This narrows accidental path races but is not a defense against
a privileged concurrent attacker. It validates the exact assertions and
re-hashes every record after writing. `PACKAGE_COMPLETE.json` is written last;
a directory without a valid completion record, exact file set, and verified
manifest is incomplete and must not be cited or submitted. The script never
writes evidence into the checkout, because doing so would make later provenance
observe a dirty source tree.

Verification is read-only. It requires the exact five-file topology, bounded
regular non-linked files, canonical strict JSON, the completion-to-manifest
binding, every manifest hash, both benchmark invariants, the summary bindings,
and, when supplied, the expected full commit. Obtain that expected commit from
a trusted checkout, release, or workflow URL; the package can bind a stated
commit but cannot authenticate its own download source. A successful verifier
run proves internal consistency with these synthetic assertions, not source
trust, elapsed-time performance, production use, security, or outside adoption.

The focused record is also expected to assert that changing a partial output
after `inspect` makes the decision stale and that rejection leaves state,
events, quarantine, the partial output, and attempt count unchanged. A refreshed
decision must then complete and verify. This is a local synthetic invariant, not
an authentication, concurrency, or production-security claim.

The pipeline comparison is expected to assert 18 child calls for naive full
restart versus 13 for BenchHandoff resume, with five versus zero repeated
successful tasks and the same final output hash. These are deterministic counts
for a synthetic 12-task fixture. They are not elapsed-time, cost, GPU,
real-workload, production, or third-party performance measurements.

## Share a bounded reproduction report

A useful report contains:

- the exact commit or released version;
- operating system and Python implementation/version;
- the exact reproduction command;
- test pass/fail/error/skip counts;
- the complete `SHA256SUMS.txt` values for the bounded JSON records;
- deviations from this guide; and
- a link to immutable evidence when one exists.

Do not share raw run directories, task logs, absolute paths, command-line
secrets, environment dumps, credentials, or private inputs. Reproduce a problem
with new synthetic inputs and share only the bounded report fields listed above.

Use the structured reproduction-report issue after the repository is public.
Submitting a report documents one attempt; it does not by itself prove
production use, broad compatibility, security, scientific validity, or ongoing
adoption. It also does not change a public count. Only an outside report that
meets [the external-evidence rules](EXTERNAL_EVIDENCE.md), receives human
review, and is merged as a verified record in `EXTERNAL_EVIDENCE.json` counts
as an independent reproduction.
