# Security Policy

BenchHandoff is a pre-release alpha, not a sandbox or security boundary. Child
commands inherit the caller's permissions and environment. Review
[LIMITATIONS.md](LIMITATIONS.md) before running untrusted or sensitive work.

The sibling writer lock serializes cooperating local BenchHandoff mutation
entrypoints. Do not use it as authorization, hostile-writer protection,
distributed fencing, or a remote lease.

## Supported versions

There is no public supported release yet and no security-support or
response-time commitment. After publication, this section must name the exact
supported release line; do not infer support from the repository default
branch.

## Reporting a vulnerability

GitHub private vulnerability reporting is enabled for this repository. Use
[Report a vulnerability](https://github.com/tiramitree/benchhandoff/security/advisories/new)
for security reports. Include the affected commit or release, impact, a
minimal reproduction, and suggested mitigations when known.

Do not use a public issue for exploit details, secrets, private data,
vulnerable run evidence, or a working proof of concept.

Do not upload raw run directories, `plan.json`, `state.json`, `bundle.json`,
`events.jsonl`, task logs, absolute local paths, environment dumps, credentials,
or tokens. These files can reveal host layout or command output. Do not assume
that editing an evidence record is safe redaction: it invalidates the evidence
bindings and can still leave identifying context elsewhere in the run tree.

## Disclosure and remediation boundary

Acknowledgement, severity, remediation, and publication timing require human
review. Do not promise a fix date before the report has been reproduced and its
scope understood. If a release is affected, preserve the original artifact,
publish a corrected version, and document whether the old release was yanked or
otherwise discouraged; never replace published bytes under an existing version.
