## Scope

Describe the smallest behavior or documentation change and why it belongs in
BenchHandoff's narrow v0.1 boundary.

## Validation

List exact commands, environment, pass/fail/error/skip counts, and any checks
not run. Do not describe a proposed CI job as observed success.

## Evidence and claim boundary

- [ ] New benchmark statements identify the exact commit, environment, command,
      raw record, and SHA-256.
- [ ] Synthetic results are not described as wall-clock, production,
      real-workload, external-adoption, or superiority evidence.
- [ ] Documentation distinguishes local validation, public CI, released
      artifacts, and independent reproduction.

## Privacy and provenance

- [ ] The change contains no private dataset, raw run directory, sensitive log,
      absolute local path, username, credential, token, or confidential
      command line.
- [ ] Every copied or adapted fragment, fixture, and third-party source is
      identified with its provenance and license.
- [ ] Material AI assistance is disclosed below, including tool, output scope,
      sources, and human verification.

AI assistance and source disclosure:

<!-- Write "None" or provide the required disclosure. -->

## Engineering checklist

- [ ] A focused regression covers changed behavior and hostile boundary cases.
- [ ] Full tests and compile checks pass, or every exception is explained.
- [ ] Command, schema, exit-code, and limitation changes are documented.
- [ ] Fail-closed behavior was not weakened merely to pass a platform check.
- [ ] The change does not assert release, adoption, support, or compatibility
      without immutable public evidence.

The project has no selected open-source license yet. Do not merge or accept an
outside contribution until the owner explicitly selects a license and the
repository metadata is updated.
