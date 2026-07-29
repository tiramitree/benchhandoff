## Scope

Describe the smallest behavior or documentation change and why it belongs in
BenchHandoff's current narrow evidence-engine or optional `AgentRun` boundary.

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
- [ ] Any external-evidence record meets the public taxonomy, relationship,
      consent, source-commit, and URL rules; its derived counts pass
      `python tools/verify_external_evidence.py`.

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
- [ ] Controller coordination changes preserve deterministic Job identity,
      fail closed on ambiguous Job/Pod sets, and distinguish Lease coordination
      from strict fencing or exactly-once execution.
- [ ] Leader-election changes retain the precreated exact-Lease boundary,
      `resourceNames`-restricted `get`/`update`, and no official artifact upload
      from pull-request execution.

The project is distributed under Apache-2.0. Do not merge copied, generated, or
adapted work unless its provenance is documented and its terms are compatible
with that license.
