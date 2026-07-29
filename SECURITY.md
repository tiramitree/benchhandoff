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

## AgentRun security boundary

The optional version 0.4 `AgentRun` controller, plus the unreleased v0.5
two-manager candidate, orchestrates the same version 3 engine through Kubernetes
Jobs. Neither makes an untrusted suite, image, cluster, or PVC safe. Any
principal that may update
`spec.resumeDecisionSHA256` can submit the exact published approval value; the
digest is a content binding, not a credential, signature, human identity, or
authorization decision. Use Kubernetes RBAC and admission policy to control
who may create or update `AgentRun` objects.

The checked-in reference/E2E manifest uses a ClusterRole and
ClusterRoleBinding. It can read `AgentRun` objects and update their status,
create and observe Jobs, and observe Pods across all namespaces. It has no
Secret permissions. A real deployment must review that cluster-wide scope and
replace or narrow it to the intended namespaces and operations.

The v0.5 candidate separately uses one `coordination.k8s.io/v1` Lease in
`benchhandoff-system`. The exact Lease is precreated. Its namespaced Role uses
`resourceNames` and allows only `get` and `update` on that object. It cannot
create, list, watch, patch, or delete Leases, update another Lease in the same
namespace, or read one across namespaces. Fixed 15/10/2-second
duration/renew/retry settings and `LeaderElectionReleaseOnCancel=false`
coordinate cooperating managers; they are not a fencing token, authorization
boundary, or proof that a former leader cannot act during a network partition.

The manager needs its Kubernetes service-account token for those API
operations; generated runner Jobs disable token mounting. Runner settings such
as non-root
execution, a read-only root filesystem, dropped capabilities, and
runtime-default seccomp narrow the generated Pod template, but they do not
restrict PVC writes, network access, workload side effects, or a hostile image.

The controller trusts live Kubernetes object identity and one bounded
termination message. Those records are not signed or remotely attested. A
principal with sufficient cluster privileges can alter resources the controller
trusts. Released v0.4 observed one manager in one single-node kind cluster. The
v0.5 candidate configures two managers, but its real-kind and public-CI gates
have not yet passed on an immutable v0.5 commit. There is no strict-fencing,
network-partition,
production-high-availability, production-isolation, or general Kubernetes
compatibility claim. See
[the AgentRun controller boundary](docs/AGENTRUN_CONTROLLER.md).

## Supported versions

Versions 0.1.0 through 0.4.0 are early GitHub version lines, not supported
production lines. Version 0.5.0 is currently an unreleased source candidate.
Security reports may identify an exact tag or full commit. There is no
response-time, remediation-time, compatibility, or maintenance commitment.

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
