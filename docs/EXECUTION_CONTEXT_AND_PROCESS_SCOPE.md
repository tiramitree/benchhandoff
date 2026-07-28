# Execution-context, process-scope, and version 3 workspace lifecycle

Suite versions 2 and 3 add fail-closed controls to the version 1 evidence
protocol. They are deliberately narrower than an environment snapshot system,
continuous monitor, or sandbox.

## Bound launch context

A version 2 suite declares one ordinary descriptor file:

```toml
version = 2
name = "context-bound-copy"

[context]
path = "context.json"
media_type = "application/vnd.example.context.v1+json"
digest = "sha256:<64 lowercase hexadecimal characters>"
size = 80
```

The path must be portable, relative, and declared as a seed task input. Before
creating the run directory, before each launch, after each child exits, and
before resume mutation, BenchHandoff requires the file bytes to match the
declared digest and size. The media type is opaque text. The runner does not
parse the descriptor semantics, fetch an external object, construct a VM or
container, or prove that the described environment is installed.

For each task, version 2 also resolves the portable `argv[0]` before execution.
It binds:

- executable basename, byte size, and SHA-256;
- SHA-256 and UTF-8 length of the normalized resolved path, without putting
  that raw path in the executable record;
- the descriptor record;
- the launch-environment policy; and
- the selected process-scope backend.

The exact resolved executable is then passed to `Popen`; it is re-resolved and
re-hashed after the child exits. Same-content executables at different paths
therefore remain distinguishable without publishing the path in this record.
Version 2 rejects an absolute `argv[0]`; use a portable bare name such as
`python` or a suite-relative path such as `bin/worker`.

Version 2 does not inherit the caller's environment. It passes only the three
derived `BENCHHANDOFF_*` control variables and, on Windows, `SystemRoot`.
`SystemRoot` is represented in the plan only by its value hash and UTF-8 byte
length. `PATH`, credentials, tokens, user-home variables, package settings,
and arbitrary caller variables are not inherited. A task that needs an
additional public configuration value should consume a declared file instead.

On Linux the child still receives the declared portable `argv[0]`. Because no
`PATH` is provided, software that derives its own executable from those two
values may not reconstruct the bound path. In particular, CPython
`sys.executable` is not guaranteed to be usable for self-relaunch. A task that
requires self-location must use an explicit declared mechanism; Linux-specific
code can query `/proc/self/exe`.

This is an evidence-bound launch context, not a package lock, filesystem
snapshot, driver inventory, hardware identity, remote attestation, or
reproducible-build guarantee.

## Process-family lifecycle

Version 2 launches the task leader through `ProcessScope` and binds the backend
policy in the plan and attempt.

### Windows

The runner:

1. creates an anonymous Job Object with `KILL_ON_JOB_CLOSE`;
2. creates the task leader with `CREATE_SUSPENDED`;
3. assigns that suspended process to the Job; and
4. resumes its sole primary thread only after assignment succeeds.

Ordinary descendants created by an in-Job process remain in the Job. A handled
failure terminates the Job and confirms it has no active members. If the runner
process exits hard, closing its last Job handle invokes the operating-system
kill-on-close behavior. The design follows the documented
[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
model; it is not a security boundary against privileged code or mechanisms
that create work outside the Job.

### Linux

The leader starts in a new session and process group. Handled cleanup sends
`SIGTERM`, waits for a bounded interval, then sends `SIGKILL` and requires the
group to become empty. After a hard runner exit, the group can remain alive.
Resume enumerates the recorded group and refuses while any non-zombie member
remains or membership cannot be determined.

This backend is explicitly cooperative. A descendant can call `setsid()` or
`setpgid()` and leave the group. It is not cgroup v2 containment and does not
claim hostile descendant control. See
[`setsid(2)`](https://man7.org/linux/man-pages/man2/setsid.2.html) and the
[Python subprocess documentation](https://docs.python.org/3/library/subprocess.html).

## Quiescence gate

A zero exit from the leader is not enough. Before hashing any output, version 2
requires the complete in-scope family to be empty. If a descendant remains
after its leader exits, the runner terminates the scope, records
`empty_confirmed=true`, fails the attempt, and does not create a bundle.

Each version 2 attempt records:

- scope mode and whether its semantics are cooperative;
- the leader-bound scope id;
- the execution-context digest;
- whether an empty scope was confirmed; and
- whether closure was natural, terminated, recovered after runner exit, or
  stopped during a confirmed launch cleanup.

Windows and Ubuntu tests create a real child and grandchild. They cover
explicit termination, leader exit with a surviving descendant, child-identity
state-write failure, and runner hard exit. These are synthetic local lifecycle
tests. They do not establish production reliability, hostile-code isolation,
real workload performance, external use, or independent review.

## Version 3 workspace checkpoints

Version 3 inherits the entire version 2 descriptor, minimal environment,
executable identity, and process-scope lifecycle. It changes the task root to
one dedicated `workspace.root` and binds a reviewed manifest stored outside
that root. The bounded persistent directory topology and ordinary-file
primary-stream bytes are observed before the run, before launch, after
quiescence, during recovery, before bundling, and during verify.

These are discrete observations of only `workspace.root`. They neither prevent
writes elsewhere nor continuously watch the tree. Device-id comparison can miss
a same-device bind mount. The protocol does not bind file mode, owner,
timestamps, ACLs, extended attributes, NTFS alternate data streams, sparse-file
layout, or other unlisted metadata. It is not a sandbox or hostile-writer
boundary.

After a normal child exit, `workspace_after` is a post-quiescence observation.
After a hard runner crash, a durable `running` attempt may lack that terminal
view; recovery observes the then-current tree before quarantine and cannot
claim it was the exact crash-time tree. Quarantine uses an atomic no-replace
primitive only on Windows, Linux, and macOS and fails closed when unavailable.

See
[`CLOSED_WORLD_WORKSPACE_INTEGRITY.md`](CLOSED_WORLD_WORKSPACE_INTEGRITY.md)
for the full workspace and privacy boundary.

## Version 1 and 2 compatibility

The reader continues to validate and execute suite/evidence schema versions 1
and 2 with their original task roots and evidence shapes. Old records do not
gain newer fields retroactively. Dispatch is strict: version 1 rejects version
2 and 3 extras; version 2 still requires context/process-scope records and
rejects workspace fields; version 3 requires both the version 2 records and its
workspace binding.

## Publication and privacy boundary

The executable record omits its raw resolved path, and static environment
values are hashed. That does not make an entire run directory safe to publish.
The suite and attempt still contain the full argument array; plan and resume
records still contain absolute suite/run paths; logs contain arbitrary child
output. Version 3 manifests reveal relative paths, kinds, sizes, and content
hashes, while workspace writer locks can reveal the absolute root and owner PID.
Review and reproduce with synthetic inputs instead of publishing raw
run evidence.
