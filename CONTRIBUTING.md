# Contributing

BenchHandoff is an early alpha distributed under
[Apache License 2.0](LICENSE). Contributions must have clear provenance and be
distributable under the repository license; opening a change does not imply
that it will be accepted.

## Before opening a change

- Keep the project narrow: a sequential, resumable command runner with
  reviewable evidence and one optional bounded Kubernetes `AgentRun`
  lifecycle. Version 0.5 adds only a fixed two-manager
  namespaced-Lease takeover boundary.
- Open an issue before a large behavioral or evidence-format change.
- Do not submit private datasets, credentials, tokens, raw run directories,
  command logs, local usernames, absolute paths, or employer/university
  confidential information.
- Declare every third-party source, copied fragment, generated fixture, and
  relevant license. Do not paste code whose provenance or reuse rights are
  unclear.
- Disclose material AI assistance in the pull request. Name the tool, describe
  what it produced, identify the human checks performed, and cite any sources
  the generated work relied on. The contributor remains responsible for the
  result.

## Development check

BenchHandoff supports Python 3.11 or newer and has no third-party runtime
dependencies. From the repository root:

```powershell
$env:PYTHONPATH = "src"
python tools\verify_license_state.py --require-final
python tools\verify_external_evidence.py
python -m unittest discover -s tests -v
python -m compileall -q src tests tools benchmarks examples
```

The optional controller requires Go 1.26:

```powershell
Push-Location controller
go mod tidy -diff
go mod verify
go vet -mod=readonly ./...
go test -mod=readonly ./...
go build -mod=readonly ./cmd/manager
Pop-Location
```

If a local environment cannot run the Go race test, it remains a required
supported-environment/public-CI gate; record the local check as not run, not as
a pass.

If Go VCS stamping selects an unrelated enclosing repository for a nested
worktree, a local link smoke may add `-buildvcs=false`. Record that exact
exception; it does not replace the ordinary clean-checkout build in public CI.

On a POSIX shell:

```bash
python tools/verify_license_state.py --require-final
python tools/verify_external_evidence.py
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests tools benchmarks examples
```

```bash
cd controller
go mod tidy -diff
go mod verify
go vet -mod=readonly ./...
go test -mod=readonly ./...
go test -mod=readonly -race ./...
go build -mod=readonly ./cmd/manager
cd ..
```

The registered real-API integration gate is
`bash controller/test/e2e/run_kind.sh`. It requires Linux, Docker, outbound
access to pinned downloads and images, and enough resources for its disposable
kind node, local registry, and two manager Pods. The v0.5 gate deletes
the current Lease holder through a UID-preconditioned request while synthetic
start is live and again after a terminal resume result remains pending behind
a temporary business-RBAC barrier. It cordons the single node, preserves the
separate Lease permissions, requires the exact pre-existing passive Pod to
acquire the next Lease transition without changing the single Job/Pod
identities, restores the exact business binding and node scheduling, and
writes two privacy-gated files only for a trusted successful upload context.
Review the script's exact bounded resource names and cleanup report before
using it on a shared Docker host.

Tests that create symlinks can be skipped when the operating system or account
does not permit symlink creation. Report skips separately from passes; never
rewrite a skipped or proposed check as observed success.

## Change requirements

1. Add a focused regression for behavior changes and hostile boundary cases.
2. Preserve fail-closed behavior for ambiguous process, path, output, and
   evidence states.
3. Keep fixtures synthetic, small, and free of private or identifying data.
4. Update documentation when a command, schema, exit code, or limitation
   changes.
5. Do not silently change an existing evidence schema. Explain compatibility
   and migration consequences.
6. Do not weaken a check merely to make a platform test pass.

## Benchmark and validation claims

Benchmark records must identify the exact commit, command, Python version,
operating system, raw output, and skipped checks. Results from
`benchmarks/synthetic` are deterministic child-work counts for synthetic
fixtures. They are not evidence of wall-clock speed, production reliability,
real-workload performance, independent adoption, or superiority over another
tool.

Use precise language:

- `observed locally` only for a command actually run and retained as evidence;
- `proposed CI matrix` until a public workflow run exists, and `public CI`
  only with the exact run URL, commit, job outcomes, and skips;
- `independently reproduced` only when an identifiable outside reproduction is
  linkable; and
- `released` or `published` only after the public artifacts exist.

## External-evidence changes

An Issue, Pull Request, CI run, star, fork, download, install, or maintainer
example does not create an adoption count. A proposed record must meet the
definitions in [docs/EXTERNAL_EVIDENCE.md](docs/EXTERNAL_EVIDENCE.md), disclose
all relevant relationships, include explicit consent, use one public HTTPS
evidence URL and one full source commit, and survive human review.

Keep a retracted record with a date and reason instead of deleting it. After
every ledger change, recompute the derived counts and run
`python tools/verify_external_evidence.py`. The validator checks structure and
arithmetic only; it does not establish identity, independence, truth, or live
URL availability.

## Pull request checklist

The pull request template asks for scope, tests, claim boundaries, provenance,
AI assistance, and privacy review. A passing test is necessary but does not by
itself establish security, scientific validity, production readiness, or
external adoption.
