# Contributing

BenchHandoff is currently an unpublished local alpha. No open-source license has
been selected, so this document describes the intended review process but does
not grant permission to copy, redistribute, or publish the project. Do not merge
or accept outside contributions until the owner has explicitly selected a
license and the repository metadata has been updated.

## Before opening a change

- Keep the project narrow: a local, sequential, resumable command runner with
  reviewable evidence.
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
python -m unittest discover -s tests -v
python -m compileall -q src tests benchmarks examples
```

On a POSIX shell:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests benchmarks examples
```

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
- `proposed CI matrix` until a public workflow run exists;
- `independently reproduced` only when an identifiable outside reproduction is
  linkable; and
- `released` or `published` only after the public artifacts exist.

## Pull request checklist

The pull request template asks for scope, tests, claim boundaries, provenance,
AI assistance, and privacy review. A passing test is necessary but does not by
itself establish security, scientific validity, production readiness, or
external adoption.
