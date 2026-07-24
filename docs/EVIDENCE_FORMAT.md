# Evidence Format v1

All JSON files use strict UTF-8, sorted object keys, compact separators, finite
JSON numbers, and a trailing newline. Hashes are lowercase SHA-256 hex digests;
sizes are byte counts. Reader and writer both enforce a 16 MiB file limit,
maximum depth 64, and maximum 100,000 JSON nodes.

## `plan.json`

The immutable plan binds:

- `run_id` and absolute run directory;
- exact `suite.toml` path, hash, and size;
- normalized suite name, version, ordered tasks, argument arrays, inputs, and
  outputs;
- SHA-256 identities for seed inputs; and
- descriptive Python and platform facts.

An input produced by an earlier task is not a seed. Its expected identity comes
from that producer's completed state.

## `state.json`

Run status is `running`, `failed`, or `completed`. Task status is `pending`,
`running`, `failed`, or `completed`. Completed tasks must form one ordered
prefix; tasks after the first incomplete task must remain untouched and pending.

Each attempt records:

- number and timestamps;
- exact argument array;
- verified pre-run input identities;
- stdout and stderr artifact paths;
- `child_launch_guard`, child PID, and stable process-start token when durably
  available;
- terminal return code, or an explicit reason when a dead identified child has
  no recoverable return code;
- verified output identities on success; and
- quarantined partial-output identities after a later resume recovers it.

The state binds the plan hash and the event-log hash, byte size, and record
count. It also carries at most one pending event intent. State is published with
a same-directory temporary file, file flush, and atomic replace.

## `events.jsonl`

Each bounded record includes the schema version, run id, UTC timestamp,
contiguous sequence number, prior complete-log SHA-256, type, optional task id,
and type-specific details. Mandatory counters are non-null integers; only a
`task_failed.return_code` may be null, including when launch failed before a child existed.

The event sequence is content-monotonic, but the file is not updated with
filesystem append. The writer reads the validated prior bytes, adds one
canonical record, and atomically replaces the complete file. Limits are 64 MiB
for the log, 100,000 records, and 256 KiB per record. The final event-log
identity is bound by both state and bundle.

## Resume decision (CLI output, not a stored run record)

`inspect` emits one canonical JSON object with kind
`benchhandoff-resume-decision`. It contains the run id and directory, the
proposed action, the completed task prefix and output identities, the exact
suite identity, all current run-evidence file identities, and a next-task view
when work remains. That view contains the task status and next attempt number,
verified input identities, unverified output presence or identity, and the
presence or identity of each deterministic quarantine candidate.

`decision_sha256` is the SHA-256 of the same canonical object with that one
self-referential field omitted. The decision is emitted to stdout and is not
written into the run directory. Passing it to
`resume --expected-decision-sha256` makes the resume conditional on an exact
recomputation before any transition.

A decision can only be issued for stable event/state evidence. It is a local
byte-identity approval token, not a secret, signature, trusted timestamp,
authorization service, or concurrency lock. Absolute run and suite paths in the
object may be identifying and should not be published without review.

## `bundle.json`

The one-time bundle contains:

- the exact suite identity;
- seed-input records from the plan;
- every completed task output identity; and
- every evidence-artifact identity for `plan.json`, `state.json`,
  `events.jsonl`, `logs/**`, and `quarantine/**`.

`verify` requires the recorded artifact file set to match exactly and separately
requires the run-root topology to contain only the three core files, the two
evidence directories, and `bundle.json`. `bundle.json` does not hash itself.

These are local integrity bindings, not a signature or trusted timestamp. See
[`LIMITATIONS.md`](../LIMITATIONS.md) for the threat and crash boundary.
