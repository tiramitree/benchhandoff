# Architecture

```text
suite.toml + declared seed files + optional v3 workspace manifest
               |
               v
        strict preflight
        - portable paths
        - regular files
        - SHA-256 identities
        - outputs absent
        - v2/v3 descriptor/executable
          and launch-policy identity
        - v3 complete workspace baseline
               |
               v
           plan.json
               |
               v
   task_started + launch guard
               |
               v
       process launch (shell=False)
       v1: direct child
       v2/v3: Job/process group
       v3: discrete workspace checks
          |               |
       non-zero         zero
          |               |
          v               v
     failed state    v2/v3 scope-empty gate
     no bundle        + completed state
          |               |
          v               |
   inspect (read only)     |
   decision SHA-256        |
          |               |
          +-- bound resume+
          | quarantine partials
          | append attempt
          v
events.jsonl + stdout/stderr + quarantine
               |
               v
          bundle.json
               |
               v
       same-CLI fresh re-hash
```

For versions 1 and 2, the suite tree contains task inputs and outputs. Version 3
uses one dedicated `workspace.root` inside the suite tree as the task root and
keeps its reviewed manifest outside that workspace. In every version, the run
tree contains ledger evidence and must be outside and separate from the suite
tree so a task output cannot overwrite its own plan or state.

## Optional AgentRun control plane

Version 0.4 adds an optional Kubernetes `AgentRun` control plane around the
same strict version 3 engine:

```text
AgentRun + immutable execution spec -> deterministic start Job
                                      | completed
                                      +-------------> verify Job -> Succeeded
                                      |
                                      | failed + decision
                                      v
                              AwaitingApproval
                                      | exact write-once digest
                                      v
                                  resume Job -> verify Job -> Succeeded
```

The Go manager binds each Job to the `AgentRun` UID, a canonical execution-spec
SHA-256, an exact audited template, the live Job UID, and one owned Pod. The
Python bridge performs one engine action over a PVC and returns a bounded,
path-free termination message. The manager does not read Pod logs, suite
bytes, or run evidence.

This layer is lifecycle orchestration, not a new evidence protocol. Version 3
still defines suite, run, workspace, resume, and bundle semantics. See
[`AGENTRUN_CONTROLLER.md`](AGENTRUN_CONTROLLER.md) for approval, restart,
security, and validation boundaries.

Version 0.5 keeps that CRD and lifecycle unchanged, but runs
the reference manager as a fixed active/passive pair:

```text
        Lease: benchhandoff-system/agentrun-controller.benchhandoff.dev
        duration 15s | renew 10s | retry 2s | release-on-cancel false
                              |
                     +--------+--------+
                     |                 |
                manager A         manager B
                  leader           standby
                     |
                     v
        fresh AgentRun + live Job/Pod API observations
                     |
                     v
          deterministic start/resume/verify Job
```

Only the Lease holder starts controller-runtime reconciliation. The separate
namespaced Lease Role is restricted by `resourceNames` to the precreated exact
Lease and grants only `get` and `update`. It grants no create, list, watch,
patch, delete, other-Lease update, or cross-namespace Lease access. The
reference ClusterRole used for AgentRun, Job, and Pod reconciliation remains a
separate broader scope.

Two windows receive explicit v0.5 handling:

```text
Create deterministic Job
        |
        +-- success or AlreadyExists
        |        |
        |        v
        |  uncached action-set List + deterministic-name GET
        |        |
        |        +-- exactly one full-template/UID match -> bind
        |        +-- ambiguity or mismatch -> Blocked
        |
        +-- unknown error -> return and begin a later fresh reconcile

Status Update
        |
        +-- success -> stable binding
        +-- conflict -> discard stale candidate -> delayed fresh reconcile
```

The registered v0.5 gate starts from a clean commit. For each paused synthetic
`start` and `resume` Job, it binds one stable Lease resource version to both
manager Pod UIDs, cordons the node, and UID-precondition deletes the holder.
Only the pre-existing non-holder may acquire exactly the next transition. The
gate then uncordons the node, restores two Ready replicas, and requires the
same measured single Job and Pod identities. This architecture assumes the API
server and its storage stay available. A Lease is coordination, not strict
fencing; this does not establish partition safety, multi-node or multi-cluster
availability, arbitrary Pod recovery, exactly-once effects, or production HA.

## Durable records

`plan.json` is immutable after creation. `state.json` is replaced atomically per
file after meaningful transitions. `events.jsonl` is content-monotonic: every
new record binds the previous complete-log SHA-256, but the implementation
publishes the full new log with a same-directory atomic replacement rather than
using filesystem append.

An event transition has three durable observations:

1. stable state and event log;
2. state containing one pending event while the prior event log is still
   present; or
3. state containing that pending event while the event log already contains it.

Resume can reconcile the two modeled pending states. This protocol is not a
cross-file atomic transaction, full event sourcing, or a sudden-power-loss
guarantee.

Before `Popen`, the attempt is durably marked with a child launch guard. After a
stable Windows or Linux process-start token is captured, state is updated with
the PID/token and the guard is disarmed. An unresolved guard is intentionally
not auto-recovered because the runner cannot prove whether the child ran.

## Version 2 launch and quiescence

Version 2 preflight hashes a declared context descriptor file and resolves each
portable executable name to one exact ordinary file. The plan binds the
executable bytes, a hash of its normalized resolved path, a non-inheriting
environment policy, and the platform process-scope policy. The same context is
recomputed before launch, after leader exit, during read-only inspection, and
before any resume reconciliation or transition.

Windows `ProcessScope` creates the leader suspended, assigns it to a
kill-on-close Job Object, then resumes the only primary thread. Linux starts
the leader in a new session/process group. An attempt records that scope as
active alongside the leader PID/token.

After the leader reaches a terminal status, the runner checks the complete
scope before re-reading any declared input or output. A nonempty scope is
terminated and the attempt fails even when the leader returned zero. Only
`empty_confirmed=true` permits a version 2 terminal attempt or output hash.

On resume after a runner exit, Windows relies on the anonymous Job's
kill-on-close contract after rechecking the leader identity. Linux additionally
enumerates the persisted process group and refuses an active or unknown group.
The Linux group can be escaped by a cooperating descendant, so this is a
quiescence gate rather than security containment. The complete boundary is in
[`EXECUTION_CONTEXT_AND_PROCESS_SCOPE.md`](EXECUTION_CONTEXT_AND_PROCESS_SCOPE.md).

## Version 3 closed-world workspace integrity

Version 3 inherits the version 2 launch context and quiescence gate. Its task
root changes from the suite directory to the dedicated `workspace.root`. The
suite binds a canonical manifest outside that root by digest and size. A
preflight double-scan must exactly match that reviewed baseline before the run
directory is created.

The workspace observer binds directory topology and ordinary-file primary-stream
bytes only under `workspace.root`, rejecting links, reparse points, hard links,
cross-device entries, unsupported types, aliases, and limit violations.
Device-id comparison cannot identify a same-device bind mount, and no directory
outside the root is inspected. Mode, owner, timestamps, ACLs, extended
attributes, NTFS alternate data streams, sparse-file layout, and unlisted
metadata are not bound.

For each task the engine derives the only legal tree from the reviewed baseline
plus earlier sealed outputs. It records `workspace_before`, launches with the
version 2 process controls, confirms scope quiescence, and records
`workspace_after` before accepting outputs. The same derivation is checked on
run load, read-only inspection, resume, bundle creation, and final verification.
These are discrete checkpoints, not continuous monitoring, filesystem access
control, a sandbox, or a hostile-writer boundary.

A normal terminal path observes after child exit. If the runner crashes while
an attempt remains durably `running`, there may be no terminal observation.
Recovery then observes the current tree immediately before quarantine, records
that recovery-time view, atomically moves regular partial outputs, and records
the clean `workspace_recovered` view. It cannot reconstruct the exact
crash-time tree. Existing observations must match; recovery never rewrites a
contradiction.

Quarantine publication uses an atomic no-replace rename: Windows `os.rename`,
Linux `renameat2(RENAME_NOREPLACE)`, or macOS
`renamex_np(RENAME_EXCL)`. If the platform is different or the native primitive
is unavailable, recovery fails closed. macOS is not otherwise a supported task
execution target.

`snapshot-workspace` exclusively creates a start-absent manifest candidate and
re-verifies it. A failure retains the candidate for review instead of deleting
possibly raced or partially written bytes. The manifest contains relative
paths, entry kinds, sizes, and content hashes; raw run records and locks may
also contain absolute host paths. See
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](CLOSED_WORLD_WORKSPACE_INTEGRITY.md).

## Cooperative writer serialization and orphan recovery

Before either public mutation entrypoint loads or creates run evidence, it
creates `.<run-name>.benchhandoff-writer-lock.json` beside the run directory
with `O_EXCL`. The canonical record binds the normalized run path, owner PID,
available process-start token, and a random nonce. The writer also holds one
automatically released kernel guard for the complete mutation: a normalized-
path named mutex on Windows or advisory `flock` on the record on Linux. It then
rechecks the exact file object and bytes before unlinking the record.

A second cooperating writer therefore stops before its load/check/mutate
sequence. A hard exit releases the kernel guard but can leave the canonical
record, so `start` and `resume` remain blocked. No wall-clock timeout breaks
that record.

`inspect-writer-lock` provides a separate mutation-free decision. It requires a
bounded canonical record and two stable owner observations. Only a definitely
dead PID or a stable live PID with a different process-start token yields
`recover-orphan`; live, unknown, changing, or identity-unverifiable ownership
yields `refuse`. Its `decision_sha256` binds the complete record, observation,
normalized run path, and deterministic tombstone path.

`recover-writer-lock` must receive that exact digest. Under a newly acquired
kernel guard it rereads the record and owner, creates or resumes one hard-link
tombstone named from the record SHA-256, proves source and tombstone are the
same file object with the same bytes, and unlinks only the source name. It
never resumes the run. A partial source-plus-tombstone state is resumable; a
foreign tombstone or unexpected extra hard link is refused.

This is cooperative local serialization and evidence-preserving recovery, not
an expiring lease, fencing protocol, signature, network-filesystem guarantee,
distributed coordinator, or hostile-writer boundary. Read-only run `inspect`
and `verify` do not acquire the writer guard.

## Review-to-execution binding

`inspect` is a mutation-free eligibility check for stable run evidence. It
re-validates the completed prefix, liveness and retry limits, then returns a
canonical `benchhandoff-resume-decision` object. Its SHA-256 binds:

- `plan.json`, `state.json`, `events.jsonl`, every current log and quarantine
  artifact, and `bundle.json` when present;
- the exact `suite.toml`;
- current completed-output identities;
- the next task's verified input identities; and
- presence or identities for unverified outputs and their deterministic
  quarantine destinations.

`resume --expected-decision-sha256` recomputes this view after load and again
immediately before the first transition. A mismatch exits before changing
state, appending an event, moving a partial output, or launching a child.
Inspection refuses a pending event/state transition because reconciling it
would mutate the evidence being reviewed; unbound resume retains the existing
reconciliation behavior.

The decision is deliberately ephemeral and has no timestamp. It is a content
binding, not authentication, a signature, or a lease. The separate writer lock
serializes cooperating mutation entrypoints across both digest checks and the
first transition, closing their local check-to-mutation race. Direct filesystem
mutation, lock removal, remote coordination, and hostile writers remain outside
the threat model.

## Exact evidence topology

Before load, resume, or verify, the run root must contain exactly:

- regular files `plan.json`, `state.json`, and `events.jsonl`;
- directories `logs/` and `quarantine/`; and
- optional regular file `bundle.json` after sealing.

Unexpected root entries, symlinks, or wrong entry types fail closed. Referenced
log and quarantine file sets are also checked against attempt records, with only
the narrowly defined empty next-attempt log pair or deterministic interrupted
quarantine move accepted during recovery.

`bundle.json` is written once after state reaches `completed`. It hashes every
regular file under `logs/` and `quarantine/` plus the plan, state, and event log.
Verification rejects missing, changed, and extra evidence files and re-verifies
the completed task outputs in the suite tree.
