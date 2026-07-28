# Security Policy

BenchHandoff is an early alpha, not a sandbox, hostile-writer boundary, or
security boundary. Child commands inherit the caller's permissions. Version 1
also inherits the caller environment; versions 2 and 3 use a minimal
non-inheriting environment. Review
[LIMITATIONS.md](LIMITATIONS.md) before running untrusted or sensitive work.

The version 2 and 3 Windows Job and cooperative Linux process group are
lifecycle controls, not hostile-code containment. They do not restrict network,
filesystem, syscalls, credentials available through the operating-system
account, or a descendant that creates work outside its assigned scope. Version
3 checks only bounded directory topology and ordinary-file primary-stream bytes
under `workspace.root` at discrete checkpoints. It does not continuously
monitor the tree, restrict writes outside that root, or make workspace contents
safe to execute. It does not bind mode, owner, timestamps, ACLs, extended
attributes, NTFS alternate data streams, sparse-file layout, or other metadata.

Sibling run and, for version 3, workspace-root writer locks serialize
cooperating local BenchHandoff mutation entrypoints. Do not use them as
authorization, hostile-writer protection, distributed fencing, or remote
leases. Device-id comparison also does not detect every same-device bind mount.

## Supported versions

Versions 0.1.0 and 0.2.0 are early GitHub releases, not supported production
lines. Version 0.3.0 is currently a candidate, not a tag or release. Security
reports may identify `v0.1.0`, `v0.2.0`, the exact candidate commit, or another
exact `main` commit. There is no response-time, remediation-time,
compatibility, or maintenance commitment.

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

Version 3 workspace manifests omit an absolute root but reveal relative paths,
entry kinds, file sizes, and content hashes. Run evidence and run/workspace lock
records can still reveal absolute local paths and owner process identifiers.
Review all of them as sensitive metadata before publication. See
[the closed-world protocol](docs/CLOSED_WORLD_WORKSPACE_INTEGRITY.md).

## Disclosure and remediation boundary

Acknowledgement, severity, remediation, and publication timing require human
review. Do not promise a fix date before the report has been reproduced and its
scope understood. If a release is affected, preserve the original artifact,
publish a corrected version, and document whether the old release was yanked or
otherwise discouraged; never replace published bytes under an existing version.
