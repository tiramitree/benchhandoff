# Closed-world workspace integrity

Suite schema version 3 adds a dedicated, reviewed workspace tree to the
version 2 execution-context and process-scope protocol. It is intended to make
undeclared filesystem drift visible at specific evidence checkpoints.

The word *closed-world* has a narrow meaning here:

> At each workspace checkpoint, the bounded directory topology and regular-file
> primary-stream bytes must equal the reviewed baseline plus the declared
> outputs that have already been sealed.

This is a discrete integrity protocol. It is not continuous monitoring, an
operating-system sandbox, or a security boundary for hostile code.
It does not bind mode, owner, timestamps, ACLs, extended attributes, NTFS
alternate data streams, sparse-file layout, or other unlisted metadata.

## Workspace layout and suite binding

A version 3 suite keeps the manifest outside the workspace that it describes:

```text
suite/
  suite.toml
  workspace.snapshot.json
  workspace/
    context.json
    input.txt
    results/
```

Create a new review candidate with:

```console
benchhandoff snapshot-workspace workspace --output workspace.snapshot.json
```

The output path must be absent, name a regular file outside the workspace, and
have an existing checked parent directory. Snapshot publication uses exclusive
creation and never replaces an existing path. The created file and its parent
identity are checked again before success is reported. The command marks its
result as requiring review.

Failure does not imply that the candidate path is absent. If writing, file
`fsync`, close, or the later file/parent recheck fails, a partial or complete
candidate can remain at the output path. The snapshot command does not
automatically delete that evidence. A later call with the same path is refused
because `O_EXCL` exclusive creation treats it as a competing path. Inspect the
retained candidate manually or choose a fresh output path; do not treat a
failed command as permission to overwrite it.

The candidate file is `fsync`ed before close, but its parent directory is not
`fsync`ed. Even a successful return does not claim directory-entry durability
across sudden power loss or storage failure.

After reviewing the relative paths, file sizes, and hashes in the candidate,
bind its exact SHA-256 and byte size in `suite.toml`:

```toml
version = 3
name = "closed-world-example"

[context]
path = "context.json"
media_type = "application/vnd.example.context.v1+json"
digest = "sha256:<64 lowercase hexadecimal characters>"
size = 80

[workspace]
root = "workspace"
manifest = "workspace.snapshot.json"
digest = "sha256:<64 lowercase hexadecimal characters>"
size = 512
policy = "closed-world-primary-stream-v1"

[[task]]
id = "produce"
argv = ["python", "worker.py"]
inputs = ["context.json", "input.txt", "worker.py"]
outputs = ["results/result.json"]
```

For version 3, task paths and the subprocess working directory are relative to
`workspace.root`. The context descriptor is therefore also inside the
workspace and must be a declared seed input. The workspace root and manifest
paths are portable suite-relative paths; the manifest must not be within the
workspace root.

Every declared output must be absent from the baseline manifest. Its parent
directory, when it has one, must already be present in the baseline. This
keeps directory topology reviewed while allowing the task to create the
declared file.

`benchhandoff inspect-workspace suite.toml` validates the manifest binding,
baseline tree, task paths, execution context, and output topology without
creating run evidence or launching a task.

## Canonical snapshot

The manifest is strict UTF-8 JSON with exactly these root fields:

```json
{
  "entries": [
    {"kind": "file", "path": "context.json", "sha256": "...", "size": 80},
    {"kind": "directory", "path": "results"}
  ],
  "kind": "benchhandoff-workspace-snapshot",
  "policy": "closed-world-primary-stream-v1",
  "schema_version": 1
}
```

The displayed JSON is expanded for readability. The stored form must use
BenchHandoff's exact canonical encoding: keys are sorted, separators are
compact, non-finite numbers are forbidden, and one trailing newline is
present. Duplicate keys, unknown fields, noncanonical bytes, invalid UTF-8,
and unsupported schema or policy values are rejected.

Manifest paths are relative, Unicode NFC, portable, and sorted by their UTF-8
bytes. Parent directories must be explicit entries. File entries bind a
lowercase SHA-256 and byte size; directory entries bind topology only.
Windows-equivalent path aliases are rejected even when the snapshot is made on
another supported platform.

The manifest deliberately contains no absolute workspace path. It does reveal
the reviewed tree's relative names, directory structure, file sizes, and
content hashes. A manifest can therefore disclose project vocabulary and
enable confirmation guesses about known content. Review it as public metadata
before publication.

## Baseline and output state

The reviewed manifest is the immutable baseline. At any checkpoint, the
expected persistent workspace is derived as:

```text
reviewed baseline
+ declared outputs sealed by completed tasks
= expected current tree
```

Missing baseline entries, undeclared entries, type changes, content changes,
or changes to previously sealed outputs fail closed. A current task's declared
output paths can be treated as volatile only at the checkpoints that inspect a
finished or interrupted attempt. Volatility is not permission for any other
path to appear.

Before a task launches, the runner records `workspace_before`. After the
process scope is closed, it records `workspace_after`. A successful task is
not sealed until its declared outputs have been individually identified and
the resulting topology-and-primary-stream state has been checked. Completed
outputs then become part of the immutable expected tree for later tasks.

The final workspace observation is bound into `bundle.json`. `verify` observes
the live tree again and requires it to match that final binding. The state
reader also derives historical `workspace_before`, `workspace_after`, and
`workspace_recovered` summaries from the manifest and recorded output
identities instead of accepting arbitrary self-consistent summaries.

These checks occur at protocol checkpoints, including preflight, task launch,
post-scope closure, recovery, bundle creation, resume inspection, and final
verification. A filesystem mutation between checkpoints is not observed until
the next checkpoint.

## Observation rules and resource limits

Each workspace observation scans and hashes the bounded tree twice. Both
ordered entry sets and their derived summary must be identical. An unstable
two-pass result is rejected.

The observer also:

- rejects a workspace root whose path crosses a symlink or reparse point;
- accepts only directories and regular files within the tree;
- rejects symlinks, reparse points, hard-linked files, and special entries;
- requires entries to report the same filesystem device as the root;
- checks directory identity before, while, and after enumeration;
- opens files without following links where the platform supports that flag;
  and
- checks opened-file and path identity again around bounded hashing.

Current limits are:

| Resource | Limit |
|---|---:|
| Manifest bytes | 4 MiB |
| Workspace entries | 10,000 |
| Relative-path depth | 32 components |
| One file | 1 GiB |
| Total regular-file bytes | 4 GiB |

The entry limit is enforced while enumerating rather than after collecting an
unbounded directory. Topology-drift errors report counts and a digest of the
affected path set instead of embedding an unbounded path list.

Two matching scans reduce accidental race ambiguity; they do not make path
inspection atomic. A privileged or hostile concurrent writer can still race
directory enumeration, file hashing, output publication, or recovery.

## Failed attempts and quarantine

A declared output from a failed or interrupted attempt is unverified. Before
recovery, the runner requires the live workspace to match the recorded
post-attempt observation. It then moves each unverified regular output to the
run's `quarantine/` directory on the same filesystem and records its source
path, quarantine artifact path, SHA-256, and size.

After quarantine, the runner observes the complete workspace again and records
`workspace_recovered`. The recovered state must equal the baseline plus outputs
sealed by earlier completed tasks. Only then can the task return to `pending`
and be retried.

Recovery is deliberately conservative:

- undeclared files are not guessed to be task outputs and are not removed;
- output directories, links, devices, and other non-regular entries block
  automatic recovery;
- cross-filesystem quarantine is unsupported; and
- any change between the recorded failed state and recovery blocks resume.

Quarantine uses an atomic no-replace rename: the destination must stay absent,
and a competing destination is never overwritten. This operation relies on
the available Windows, Linux, or macOS no-replace rename primitive. If the
platform does not provide the required primitive, recovery fails closed rather
than falling back to a check-then-replace move.

There is one narrow recovery-time binding distinction. Durable state can
remain `running` when the runner dies before committing the post-exit summary.
It can also remain `running` when post-exit observation raises an evidence
error without a stable tree summary, even though the child may already have
exited. In that second case, the observation failure is returned to the caller
but is not currently committed as a durable attempt error or event.

Resume must first establish that the recorded child and managed process scope
are empty. After the condition that prevented a stable observation has been
manually resolved, resume may bind the then-current workspace observation as
the interrupted attempt's `workspace_after` before quarantine. This is a
recovery-time observation, not proof of the exact tree that existed immediately
after the child exited.

This exception does not apply to a durably committed terminal attempt. Every
`failed`, `interrupted`, or `completed` version 3 attempt must already contain
`workspace_after`; a missing terminal summary is rejected instead of
reconstructed.

Resume remains at-least-once, not exactly-once. Tasks must be idempotent and
must not depend on undeclared side effects. Quarantine preserves evidence of
the prior attempt; it does not prove that retrying an external side effect is
safe.

## Workspace writer lock

Version 3 mutation holds both the run writer lock and a second cooperative
writer lock keyed to the workspace root. The workspace lock serializes
`start` and `resume` operations that target the same reviewed tree even when
they use different run directories.

This lock coordinates cooperating local BenchHandoff entrypoints only. It
does not prevent another program, another account with access, or privileged
code from modifying the tree. A hard runner exit can leave the lock record
behind; later mutation fails closed until the existing writer-lock
inspection/recovery protocol determines that explicit recovery is allowed.
Read-only inspection and verification do not turn this cooperative lock into
continuous exclusion.

The lock record identifies its target with an absolute path. An orphan record,
its recovery tombstone, writer-lock inspection/recovery output, and related
CLI errors can therefore expose local path and process information. These
artifacts are not path-redacted and require the same publication review as run
evidence.

## Security and coverage boundaries

Schema version 3 retains the version 2 launch boundary:

- the execution-context descriptor and resolved executable are byte-bound;
- the child receives the minimal version 2 environment rather than the
  caller's full environment; and
- ordinary descendants are managed by a Windows Job Object or a cooperative
  Linux process group.

The version 2 limitations remain. There is no network, syscall, GPU,
credential, resource, or operating-system permission isolation. Windows
mechanisms that create work outside the Job and Linux descendants that leave
their process group remain outside the managed process scope. Version 3 does
not turn these controls into hostile-code containment.

Only `workspace.root` receives closed-world tree observation. The manifest,
suite file, context descriptor, executable, run evidence, and declared inputs
have their individual bindings where documented, but other filesystem paths
outside the workspace are not monitored as a tree. A task can still read or
write outside the workspace according to its operating-system permissions,
and it can still contact the network.

The same-device check is not a mount-namespace proof. In particular, a bind
mount that reports the same `st_dev` as the workspace root may not be detected
as a filesystem-boundary crossing. Use an external container, VM, account
boundary, or sandbox when filesystem confinement or hostile-code isolation is
required.

SHA-256 establishes byte identity under the observation protocol. It does not
establish semantic correctness, provenance ownership, safe content,
scientific validity, or external adoption.

## Publication and privacy

The workspace manifest omits absolute host paths, but a complete run directory
is not path-redacted. Run evidence can contain absolute suite and run paths,
local usernames, drive letters or mount points, process identifiers, and the
task's full argument array. Logs can contain arbitrary child output, including
secrets. Snapshot and workspace commands can also include absolute paths in
errors even though a successfully created canonical manifest does not.

Do not publish raw run evidence merely because the workspace manifest looks
safe. Review the manifest, suite, arguments, logs, quarantine artifacts,
writer-lock records and tombstones, CLI output, and every run record
separately. No raw evidence or command output is claimed to be path-redacted.
Editing a record in place invalidates its evidence bindings and is not a
redaction mechanism; reproduce a synthetic, publication-safe run instead.
