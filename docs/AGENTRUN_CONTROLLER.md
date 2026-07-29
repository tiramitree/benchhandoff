# AgentRun controller

BenchHandoff v0.4 adds an optional Kubernetes control plane around the existing
version 3 evidence engine. It is an early-alpha reference controller for one
bounded lifecycle:

```text
start Job
  | completed --------------------------> verify Job -> Succeeded
  | failed + decision -> AwaitingApproval
                           | exact approval -> resume Job -> verify Job -> Succeeded
```

Any ambiguous identity, evidence, workload result, or approval transition moves
the `AgentRun` to `Blocked`. The controller does not make BenchHandoff a
distributed scheduler, sandbox, exactly-once executor, or production service.

Version 0.5 runs a fixed pair of managers behind one
namespaced leader-election Lease and adds two registered holder-deletion
takeover gates. This is an active/passive reference experiment, not a
production-HA claim.

## Components

The implementation has five deliberately small parts:

1. `AgentRun` is a namespaced `control.benchhandoff.dev/v1alpha1` custom
   resource. Its immutable execution spec identifies a PVC, normalized suite
   path, exact suite SHA-256, digest-pinned runner image, and bounded Job
   deadline.
2. Exactly two Go manager Pods compete for the namespaced
   `coordination.k8s.io/v1` Lease
   `benchhandoff-system/agentrun-controller.benchhandoff.dev`. The fixed
   duration/renew/retry settings are 15/10/2 seconds and
   `LeaderElectionReleaseOnCancel` is false.
3. The active Go manager reconciles one deterministic Kubernetes Job at a
   time. It watches `AgentRun` and owned Job objects, and uses uncached API
   reads for Job and Pod identity decisions.
4. The Python `benchhandoff.controller_step` bridge performs exactly one
   `start`, `resume`, or `verify` action against the PVC. It emits a canonical,
   path-free JSON result through the Kubernetes termination-message channel.
5. The PVC contains `suites/` and `runs/`. The run directory is keyed by the
   Kubernetes-assigned `AgentRun` UID, so a deleted and recreated object cannot
   silently adopt the prior run.

The manager does not read runner logs, suite bytes, run evidence, or other PVC
contents. It accepts only the bounded termination protocol and separately
checks the live Job and Pod identities that produced it.

`AgentRun` objects are namespaced, but the checked-in reference manager uses a
ClusterRole and ClusterRoleBinding to watch all namespaces. That reference
scope is for the disposable gate, not a least-privilege production policy.
Operators must review and narrow it for their own deployment boundary. The
candidate's separate leader-election Role is namespaced to
`benchhandoff-system`. The exact Lease is precreated, and the Role uses
`resourceNames` to permit only `get` and `update` on it. It cannot create,
list, watch, patch, or delete Leases, update another Lease in the namespace, or
read a Lease in another namespace.

## API

A minimal resource has this shape:

```yaml
apiVersion: control.benchhandoff.dev/v1alpha1
kind: AgentRun
metadata:
  name: example
spec:
  execution:
    pvcName: benchhandoff-data
    suitePath: example/suite.toml
    suiteSHA256: <64-lowercase-hex-characters>
    runnerImage: registry.example/benchhandoff-runner@sha256:<64-lowercase-hex-characters>
    activeDeadlineSeconds: 600
```

The suite must be a strict BenchHandoff version 3 suite below
`/benchhandoff-data/suites`. The runner stores evidence below
`/benchhandoff-data/runs/<agent-run-uid>`.

`spec.execution` is immutable. The controller hashes a versioned canonical JSON
representation of all five execution fields and binds that digest into Job
names, annotations, argv, status, and the runner result. Job labels separately
bind the run UID and action.

`runnerImage` must use `name@sha256:<digest>` form. A tag-only reference is
rejected. The image digest binds image-manifest bytes; it does not prove image
provenance, safety, architecture compatibility, or the absence of mutable
external dependencies used by the workload.

## Approval flow

When the start Job records a failed BenchHandoff run, the bridge performs a
read-only `inspect` and returns the exact resume-decision SHA-256. The
controller publishes it in:

```text
.status.phase = AwaitingApproval
.status.resumeDecisionSHA256 = <decision>
```

An operator can copy that exact value into the spec:

```bash
decision="$(kubectl get agentrun example \
  -o jsonpath='{.status.resumeDecisionSHA256}')"
kubectl patch agentrun example --type=merge \
  -p "{\"spec\":{\"resumeDecisionSHA256\":\"$decision\"}}"
```

The first update is admitted only when the old status is
`AwaitingApproval` and the value equals the old status decision. The spec field
is then write-once: later change or removal is rejected.

Kubernetes transition rules that reference `oldSelf` do not run on object
creation. Therefore, a create request that already contains
`resumeDecisionSHA256` is not rejected by CEL admission. The controller detects
that state before creating a Job and moves the object to `Blocked` with reason
`PreseededApproval`. The digest is review data, not a secret, signature, user
identity, or authorization token. RBAC and admission policy remain the cluster
operator's responsibility.

The resume bridge gives the same digest to BenchHandoff's
`--expected-decision-sha256` boundary. BenchHandoff rechecks the decision before
its first resume transition. A changed suite, completed output, partial output,
or other bound evidence therefore blocks the approved resume.

## Job and result bindings

For each action, the manager accepts only one live Job that matches the
deterministic template:

- exact `AgentRun` owner reference, including UID;
- exact action and run-UID labels;
- exact run UID, action, and execution-spec annotations;
- exact digest-pinned image and controller-generated argv;
- one completion, one parallel worker, zero retries, and the registered active
  deadline;
- disabled service-account token mounting and service links;
- non-root UID/GID 65532, read-only root filesystem, dropped capabilities,
  runtime-default seccomp, and no privilege escalation; and
- one writable PVC mount plus one `emptyDir` temporary directory.

API-server-generated Job selector fields are normalized narrowly before the
template comparison. Other execution-affecting drift blocks reconciliation.

The controller records the live Job name, Kubernetes UID, and action in
`status.activeJobRef`. It refuses missing, additional, replaced, foreign-owned,
or template-drifted Jobs. It also requires at most one matching Pod while a Job
is active and exactly one Job-owned Pod before consuming a terminal result.
Duplicate or foreign matching Pods block the run even while the resource is
waiting for approval.

Job creation does not become trusted merely because `Create` returned success.
After create success or `AlreadyExists`, the candidate performs an uncached
action-label list and an uncached deterministic-name GET. It requires exactly
one expected Job, agreement on the API-server-assigned UID, the complete
registered template, and a non-ambiguous Pod set before writing
`activeJobRef`. An unknown create error is returned for a later full reconcile;
the controller never selects a second name as a recovery shortcut.

If a status update conflicts, the candidate discards that stale in-memory
status and requeues a complete fresh reconcile. A subsequent pass may treat an
already-bound reference as idempotent only when the validated name, action, and
Job UID are unchanged. A distinct UID blocks the resource.

The create/`AlreadyExists`, committed-response-loss, and status-conflict cases
are bounded fake-client fault-injection unit tests. The real-kind takeover gate
starts after `activeJobRef`, its Job, and its Pod are established, so it tests
continuity across holder deletion rather than leader loss inside those earlier
windows.

The runner's `benchhandoff-controller-step/v1` termination message is canonical
JSON capped at 1 KiB. It contains only registered enum values and bounded
identifiers:

- action and outcome;
- `AgentRun` UID and execution-spec SHA-256;
- BenchHandoff run ID;
- resume-decision SHA-256 when applicable;
- bundle SHA-256 after completion; and
- a coarse registered error code on failure.

It contains no suite path, PVC name, local path, command output, exception
text, or arbitrary log content. A distinct verify Job must re-open and verify
the bundle and return the same run and bundle identities before the controller
sets `Succeeded`.

## Manager restart and v0.5 takeover behavior

Job names are deterministic functions of the run UID, action, and canonical
execution-spec digest. Status also records the live Job UID. After a manager
restart, reconciliation re-lists the live objects and adopts only the single
exact match. It does not create a second Job merely because informer state was
lost.

Version 0.4 tested that restart/adoption behavior for one manager in one kind
cluster. Version 0.5 changes the reference Deployment to exactly two managers
using the fixed Lease above. Its registered gate removes the current
Lease-holder manager once while `start` is live. For `resume`, it temporarily
removes the business ClusterRoleBinding while retaining Lease permissions,
requires the Job result to become terminal while `AgentRun` status still binds
that resume Job, and then removes the holder. The gates require:

- a clean exact source commit and two running Ready manager Pods;
- one stable Lease resource version bound to the exact holder and non-holder
  Pod names and UIDs;
- a cordoned single node and a UID-preconditioned holder deletion;
- the previously observed non-holder, with the same UID, acquiring exactly the
  next Lease transition;
- node uncordon and restoration of two Ready replicas;
- the same measured Job and Pod names and UIDs across takeover;
- measured one-Job and one-Pod cardinality before and after.

The `start` runner is released only after its live takeover passes. The
`resume` runner is released while business permissions are denied to both
managers; the gate records the successful terminal Job and Pod, result digest,
unchanged pending `AgentRun` binding, and absent verify workload. After the
passive manager acquires the Lease, the harness restores and verifies the
registered RBAC shape, then observes successor convergence. The final
lifecycle still requires exact approval and a separate verify Job. The gate
assumes one kind node and continuously available API-server storage. It does
not exercise network partitions, a stalled-but-running old leader, storage
fencing, arbitrary runner Pod loss, multiple nodes, or multiple clusters.

## Validation

### Registered validation procedure

This source tree does not carry forward commit, run, artifact, or Release
identifiers from superseded history. The ordinary CI and real-kind workflow
pages must be checked for a run whose source revision exactly matches the
commit or tag being evaluated.

The registered real-kind gate runs Go formatting, module verification, vet,
race tests, and one bounded real-API lifecycle. It covers a deliberate
version 3 task failure, exact approval, bound resume, fresh verification, a
live-start takeover, a terminal-resume/pending-status takeover, declared
suite-digest mismatch, wrong approval, duplicate matching Pod rejection, and
the generated runner security context. It emits only
`takeover-evidence.json` and `SHA256SUMS` to a new bounded temporary evidence
directory after the relationship, topology, and privacy checks pass.

These are maintainer-operated synthetic procedures for one disposable
single-node environment. Even a successful exact-revision execution is not
production use, independent reproduction, performance evidence, external
adoption, partition-safety evidence, a compatibility matrix, or production
high availability. Earlier create/response-loss and status-conflict windows
remain fake-client unit-test coverage; the real-kind takeover begins only
after the `AgentRun`, Job, and Pod binding exists.

CI supplies a new `E2E_EVIDENCE_DIR` outside the script scratch root so the
validated files can survive scratch cleanup for upload. A local run that omits
that variable writes under `$SCRATCH_ROOT/evidence`; successful cleanup
removes that default. Retaining local evidence requires a new, non-existing
path under the temporary root.

## Reproducing the registered gate

The complete disposable gate is:

```bash
bash controller/test/e2e/run_kind.sh
```

For the v0.5 workflow, CI supplies a new temporary
`E2E_EVIDENCE_DIR`. A manual run may omit it and let the script choose a new
directory below its bounded temporary root. The script refuses an existing
evidence path and refuses a path outside that temporary root. Do not publish
raw Kubernetes objects, Pod logs, private suite data, or the scratch tree.

It requires Linux, Bash, Docker, outbound access to the pinned public
downloads and images, and enough local resources for one kind node and image
builds. It creates randomized, bounded names and removes only resources whose
creation and identity it confirmed. If ownership, API reachability, deletion,
or absence verification is uncertain, cleanup fails closed and reports the
exact bounded resource names instead of deleting ambiguous paths.

For Go-only checks:

```bash
make -C controller verify
```

The checked-in `controller/config/default` manifests are test/reference
manifests, not an installer. In particular, the manager image is an E2E
placeholder. This repository publishes no controller image, Helm chart,
upgrade operator, support policy, or production configuration.

## Explicit non-claims

The controller does not provide:

- exactly-once task or side-effect execution;
- task sandboxing, network isolation, workload authorization, or secret
  management;
- PVC snapshots, storage fencing, remote leases, or protection from a hostile
  writer;
- strict fencing, network-partition safety, arbitrary Pod recovery, multi-node
  or multi-cluster availability, or production high availability;
- automatic retry after `Blocked`, Job deletion, or evidence repair;
- arbitrary DAGs, queues, priorities, scheduling policy, autoscaling, or cost
  control;
- signed evidence, remote attestation, or verified image provenance; or
- production reliability, external use, independent review, or a support
  commitment.

The underlying BenchHandoff limitations still apply. In particular, resume is
at-least-once, version 3 observations are discrete, and child commands retain
the PVC and operating-system permissions assigned by the cluster.
