# Architecture

```text
suite.toml + declared seed files
               |
               v
        strict preflight
        - portable paths
        - regular files
        - SHA-256 identities
        - outputs absent
               |
               v
           plan.json
               |
               v
   task_started + launch guard
               |
               v
       subprocess (shell=False)
          |               |
       non-zero         zero
          |               |
          v               v
     failed state    verify inputs/outputs
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

The suite tree contains task inputs and outputs. The run tree contains ledger
evidence and must be a separate directory tree so a task output cannot overwrite
its own plan or state.

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
binding, not authentication, a signature, a cross-process lock, or a lease.
Runs still assume one trusted writer, and a small check-to-mutation race remains
outside the v0.1 threat model.

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
