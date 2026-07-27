# Evidence Format v1 and v2

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

Schema version 2 additionally requires:

- `suite.context`, containing the portable descriptor path, media type,
  `sha256:` digest, and byte size;
- that descriptor's exact identity in `seed_inputs`; and
- `execution_context`, which binds the descriptor, every task executable's
  basename/content/path-hash identity, the non-inheriting environment policy,
  and the Windows Job or cooperative Linux process-group policy.

The executable path itself is not stored in that nested record; only its
normalized-path SHA-256 and UTF-8 length are. Existing absolute suite/run paths
elsewhere in the plan remain unchanged and potentially identifying.

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

Each version 2 attempt also records the task execution-context SHA-256 and one
strict `process_scope` object. The scope contains:

- `mode`: `windows-job` or `posix-cooperative-process-group`;
- the matching `cooperative` boolean;
- `scope_id`, equal to the leader PID once launch is identified;
- `empty_confirmed`; and
- `closure`: one of the bounded launch, active, natural-empty, terminated, or
  recovered-empty states.

A running identified attempt must have an active nonempty scope record. A
terminal attempt with a child must have `empty_confirmed=true`; a safe failure
before an identified child must record that no scope was created or that a
partially created launch was cleaned.

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

For version 2, the next-task view also carries the freshly recomputed execution
context. Context drift therefore changes or blocks the decision before resume
mutation.

`decision_sha256` is the SHA-256 of the same canonical object with that one
self-referential field omitted. The decision is emitted to stdout and is not
written into the run directory. Passing it to
`resume --expected-decision-sha256` makes the resume conditional on an exact
recomputation before any transition.

A decision can only be issued for stable event/state evidence. It is a local
byte-identity approval token, not a secret, signature, trusted timestamp,
authorization service, or concurrency lock. Absolute run and suite paths in the
object may be identifying and should not be published without review.

## Writer-lock recovery decision and tombstone

`inspect-writer-lock` emits a canonical
`benchhandoff-writer-lock-recovery-decision` object without writing a file. It
contains the normalized run and lock paths, exact lock identity and parsed
record, a stable owner observation, action and reason, and deterministic
SHA-named tombstone path. `decision_sha256` hashes the same object with that
field omitted.

The lock reader is independently bounded to 4096 bytes and requires the exact
schema and canonical bytes. A recovery action is emitted only for a definitely
dead owner or a stable live PID whose process-start token differs from the
record. The digest is a local content binding, not a secret, signature, lease,
trusted time, or proof that resuming the run is safe.

`recover-writer-lock --expected-decision-sha256` recomputes the decision under
the platform kernel guard. It preserves the original canonical lock bytes as a
hard-linked sibling named from the lock SHA-256, verifies file-object identity,
content identity, and exact link count, then removes only the active source
name. Its emitted `benchhandoff-writer-lock-recovery` result records the
decision and tombstone identities. The result is CLI output, not a run record.
The tombstone itself remains the original lock record and is not included in
`bundle.json`.

## `bundle.json`

The one-time bundle contains:

- the exact suite identity;
- seed-input records from the plan;
- every completed task output identity; and
- every evidence-artifact identity for `plan.json`, `state.json`,
  `events.jsonl`, `logs/**`, and `quarantine/**`.

Version 2 also copies the immutable plan execution-context object into the
bundle and requires exact equality during verification. Version 1 bundles
retain their original exact key set and remain readable.

`verify` requires the recorded artifact file set to match exactly and separately
requires the run-root topology to contain only the three core files, the two
evidence directories, and `bundle.json`. `bundle.json` does not hash itself.

These are local integrity bindings, not a signature or trusted timestamp. See
[`LIMITATIONS.md`](../LIMITATIONS.md) for the threat and crash boundary.
