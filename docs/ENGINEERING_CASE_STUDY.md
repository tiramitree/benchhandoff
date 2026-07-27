# Engineering Case Study: Fail-Closed Recovery for Sequential AI Evaluations

An expensive evaluation pipeline rarely fails at a convenient boundary. A
model server can disappear after five successful stages, a simulator can write
half an output before exiting, or the runner itself can stop between launching
a child and recording its identity. “Skip the files that already exist” is
fast, but it cannot answer the questions that matter after such a failure:

- Which inputs and commands were actually accepted?
- Which completed outputs still match the bytes that were verified?
- Did an apparently incomplete task run, and could it still be alive?
- Is a retry safe, or would it silently duplicate side effects?
- Did the evidence approved for recovery stay byte-identical until execution?
- What evidence was preserved instead of rewritten?

BenchHandoff is a deliberately narrow answer for a flat, sequential batch. It
does not schedule a DAG, isolate hostile code, or make scientific results
reproducible by itself. Its purpose is to turn recovery from a filename
heuristic into a reviewable state transition.

## The design target

The input is a `suite.toml` with ordered tasks. Each task declares an argument
array, inputs, and outputs. Before the first child starts, the runner validates
portable paths, rejects symlinks and non-regular seed files, requires outputs
to be absent, and hashes every seed input. Four records then have distinct roles:

| Record | Role |
|---|---|
| `plan.json` | Immutable accepted suite, source identities, task order, and bounded runtime facts |
| `state.json` | Current task and attempt state, plus the event-log identity and any pending transition |
| `events.jsonl` | Content-monotonic transition history, each version binding the prior complete-log hash |
| `bundle.json` | Written only after completion; binds the final plan, state, event log, logs, quarantine, and outputs |

The central invariant is a **completed prefix**: completed tasks must form one
ordered prefix, and later tasks must remain untouched. On load, resume verifies
the ledger structure, seed identities, and expected regular-file sets beneath
logs and quarantine. It reconciles a modeled pending event when necessary, then
re-verifies completed outputs before recovering or launching a task.

```text
accepted plan
     |
     v
completed prefix -----> first incomplete task -----> untouched suffix
     |                           |
re-hash inputs/outputs       inspect liveness,
and prior evidence           retry budget, logs,
                             and partial outputs
```

This is intentionally less flexible than a general workflow engine. The
restriction makes it possible to state and test exactly what a resume may
preserve, quarantine, or retry. For a reviewed recovery, `inspect` derives a
second invariant: the resume is authorized only while the exact evidence,
relevant inputs, completed outputs, and partial-output observations still hash
to the reviewed decision SHA-256.

## A concrete failure timeline

The fixed comparison fixture contains 12 sequential tasks. Task 6 fails on its
first call after tasks 1–5 have completed.

1. `start` records each successful task and verifies its declared output.
2. Before launching task 6, the runner durably arms a child-launch guard and
   creates dedicated stdout/stderr artifacts. v0.1 does not cap their size.
3. Task 6 writes a partial declared output and exits `75`.
4. The run becomes `failed`; no `bundle.json` is created. The partial file is
   not promoted into verified output evidence.
5. `inspect` performs the load, completed-prefix, liveness, and retry checks
   without reconciling or writing evidence. It emits a decision SHA-256 over
   the evidence files, suite, completed outputs, next inputs, partial outputs,
   and deterministic quarantine candidates.
6. Bound `resume` requires that digest, recomputes it after load and immediately
   before its first transition, and only then identifies task 6 as recoverable.
7. The partial regular file moves into `quarantine/` with its identity
   preserved. A second attempt starts, then tasks 7–12 run in order.
8. Only after every declared output verifies does the runner seal
   `bundle.json`; a fresh `verify` invocation re-hashes the complete declared
   evidence set.

The package entrypoint in `benchmarks/synthetic/reproduce.py` executes both the
focused interruption case and the 12-task comparison from one clean commit. It
refuses dirty source, overwrite, and output inside the checkout; then it writes
two raw JSON records, a bounded summary, `SHA256SUMS.txt`, and a
`PACKAGE_COMPLETE.json` record written last. The focused fixture deliberately
changes its partial output after inspection. The stale decision must be
rejected while state, events, quarantine, partial output, and attempt count stay
unchanged; a refreshed decision then completes and verifies.

## What the synthetic comparison measures

For the fixed fixture:

| Strategy | Child calls | Repeated successful tasks | Final output identity |
|---|---:|---:|---|
| Naive full restart | 18 | 5 | Same fixed SHA-256 identity |
| BenchHandoff resume | 13 | 0 | Same fixed SHA-256 identity |

The difference is exactly the five already-completed tasks. These are
deterministic **child-work counts**, not elapsed-time, cost, GPU utilization,
model quality, real-workload throughput, or a production reliability rate.
Maintainer-generated records are also not independent reproduction or adoption.
The exact commit, environment, assertions, record hashes, and claim boundary
live in each reproduction package rather than in this narrative alone.

## Five choices that deliberately fail closed

### 1. An unresolved launch guard blocks automatic recovery

The runner writes a launch guard before `Popen`, then replaces it with a stable
Windows or Linux process-start identity after launch. If the runner dies in
that window, it cannot prove whether the child ran or whether undeclared side
effects occurred. Guessing “probably dead” would make a retry convenient but
unauditable, so resume stops for manual review or abandonment.

This is at-least-once recovery, not exactly-once execution. Tasks must still be
idempotent and avoid undeclared side effects.

### 2. The event log is replaced as a complete file

Each transition binds the SHA-256 of the prior complete log. The implementation
publishes the new complete JSONL file with same-directory atomic replacement
instead of appending in place. A pending-event intent in `state.json` lets
resume reconcile the two explicitly modeled interrupted-write observations.

The tradeoff is cost: rewriting grows linearly with the current log and
quadratically over a very long run. v0.1 bounds record size, record count, and
total log bytes rather than pretending this design scales without limit. It is
not a cross-file transaction or a sudden-power-loss guarantee.

### 3. Unexpected evidence files are errors

Verification requires an exact run-root, log, and quarantine topology. Ignoring
an extra file would make the bundle easier to accept, but it would also leave
unclear whether the extra artifact came from a crashed write, a task, or later
tampering. v0.1 refuses to auto-delete or silently exclude it.

### 4. A reviewed recovery decision becomes stale on byte drift

`inspect` does not write an approval record into the run. It returns a canonical
view whose digest can be carried by a human review, ticket, or higher-level
agent approval step. If a log, state file, relevant input, completed output, or
partial output changes, the digest changes and bound resume stops before the
`run_resumed` event, quarantine move, or child launch.

This closes a practical stale-approval gap. The digest is unsigned and
optional, so it must not be described as cryptographic authorization. A
separate `O_EXCL` sibling writer lock and automatically released kernel guard
span both digest checks and the complete mutation path. The deterministic
contention benchmark holds them in a second process, observes one rejected
resume with zero run-evidence file changes and no added attempt, then releases
them and completes attempt 2. The lock coordinates cooperating local
entrypoints only; it is not hostile-writer, remote-lease, network-filesystem,
or distributed-scheduler protection.

### 5. An orphan lock is recovered only through a bound preservation step

A hard runner exit releases the Windows named mutex or Linux `flock`, but the
canonical sibling record remains and still blocks mutation. BenchHandoff does
not infer orphanhood from age. `inspect-writer-lock` samples liveness and
process-start identity twice; it refuses live, unknown, changing, and
identity-unverifiable owners.

When the owner is definitely dead or the PID was stably reused, the inspection
digest binds the exact record and proposed tombstone. Bound recovery reacquires
the kernel guard, repeats the decision, hard-links the original bytes to a
SHA-named tombstone, verifies file-object identity and link count, and then
removes only the source name. Recovery and run resume remain separate actions.
The partial state with both hard-link names is itself resumable after a second
recovery-process crash.

This narrows a real unattended-agent operations gap without claiming that the
orphaned task is safe to retry. A malformed record, foreign tombstone,
unexpected link, or ambiguous owner still stops for manual evidence review.

## Version 2: launch context and descendant quiescence

Version 1 could prove declared file identity while still resolving `argv[0]`
through an unbound caller environment and controlling only the direct child.
That left two practical gaps: the same task could resume under a different
executable, and a leader could exit while a descendant continued mutating an
output.

Version 2 addresses those gaps within the declared cooperative scope without
claiming a full environment snapshot. It byte-checks one declared context
descriptor, binds the resolved executable's content and hashed path identity,
and starts from a minimal non-inheriting environment. The descriptor remains
opaque; it does not prove package, driver, container, hardware, or
external-object state.

The runner also owns a process scope. Windows assigns a suspended leader to a
kill-on-close Job before resume. Linux launches a new session/process group and
uses cooperative TERM/KILL cleanup. A leader's zero return code is insufficient:
the in-scope process family must be empty before output hashing.
Process-spawning synthetic tests spawn a grandchild and inject leader exit,
state-write failure, and runner hard exit.

This is lifecycle evidence, not a sandbox. A Linux descendant can deliberately
leave its group, Windows work can be created outside the Job through other
mechanisms, and neither backend restricts filesystem, network, syscalls, or
account permissions.

## A feature that was not shipped

A diagnostic-export command was prototyped during local hardening and then
removed before the candidate commit. A separate Codex-assisted local static
review found unresolved Windows output-parent race and reparse-point risks,
possible cross-generation evidence, and unbounded verification I/O. The
narrower repository reproduction package does not read or export arbitrary run
directories; it generates only fixed synthetic records in a newly created
directory outside the checkout.

Withholding the broader command is part of the result: a useful interface is
not ready merely because its happy path works.

## Evidence map

| Claim | Inspectable source | What it does not prove |
|---|---|---|
| Completed-prefix and recovery rules | `src/benchhandoff/engine.py`, `tests/test_state_machine_hardening.py`, `tests/test_transition_recovery.py` | Correctness outside tested states |
| Root-entry, regular-file-set, path, and non-overwrite boundaries | `src/benchhandoff/storage.py`, `tests/test_storage_hardening.py`, `tests/test_audit_regressions.py` | Protection from a privileged concurrent attacker or a complete Windows reparse-point audit |
| Fixed failure-to-resume behavior | `examples/recovery_pipeline/`, `tests/test_recovery_example.py` | Real model or simulator integration |
| Stale reviewed-decision refusal without mutation | `tests/test_resume_decision.py`, focused reproduction-package JSON | Signature security, locking, or hostile concurrency |
| Cooperative local writer contention | `src/benchhandoff/writer_lock.py`, `tests/test_writer_lock.py`, `benchmarks/synthetic/run_writer_contention.py` | Remote lease, fencing, network-filesystem proof, hostile-writer protection, or production reliability |
| Evidence-bound orphan-lock recovery | `src/benchhandoff/writer_lock.py`, `tests/test_writer_lock_recovery.py`, `tests/test_writer_lock_recovery_boundaries.py`, `benchmarks/synthetic/run_writer_recovery.py` | Safe child retry, distributed recovery, hostile-writer protection, or production reliability |
| Version 2 context binding | `src/benchhandoff/model.py`, `src/benchhandoff/engine.py`, `tests/test_execution_context.py` | VM/container snapshot creation, package/driver capture, remote attestation, or reproducible builds |
| Process-family quiescence | `src/benchhandoff/processes.py`, `tests/test_process_scope.py`, `tests/test_process_scope_engine.py` | Sandbox security, hostile descendant containment, cgroup semantics, or production reliability |
| 18→13 and 5→0 counts | Reproduction-package JSON plus `SHA256SUMS.txt` | Wall-clock speedup or external use |
| Record semantics | `docs/EVIDENCE_FORMAT.md`, `docs/ARCHITECTURE.md` | Signed provenance or trusted time |
| Known failure and threat boundaries | `LIMITATIONS.md` | Security certification |
| Authorship process | `AI_ASSISTANCE.md`, `CLEAN_ROOM.md` | Independent unaided implementation |

## Authorship and ownership boundary

The user set the direction and acceptance boundary for an auditable recovery
harness. OpenAI Codex assisted by translating those requirements into the local
implementation and by drafting source, tests, examples, and documentation.
That assistance is disclosed because a technical interview should evaluate the
user's ability to explain, challenge, validate, and extend the system—not rely
on an implication of unaided keystroke authorship.

No private research implementation was copied into this repository. The
clean-room boundary is recorded in `CLEAN_ROOM.md`.

## Relevance—and its current limit

The same failure shape appears in agent evaluations, model benchmarks, and
embodied-simulation batches: expensive ordered stages, partial outputs, retries,
and pressure to preserve evidence. Approval-gated agent harnesses add one more
problem: an approval can become stale before execution. BenchHandoff
demonstrates systems reasoning about both shapes without claiming to be a full
agent runtime.

It has **not** been deployed in an agent stack, integrated with Genie Sim,
validated on a GPU workload, or adopted by an external user. Version 2
code-bearing commit `pre-rewrite-commit-retired` completed all
ten jobs in public main-branch
[run pre-rewrite-run-retired](https://github.com/tiramitree/benchhandoff/actions):
eight Ubuntu 24.04 and Windows Server 2025 jobs across CPython 3.11 through
3.14, the canonical synthetic-evidence job, and the exact-distribution job.
That is maintainer-operated synthetic validation, not production, independent,
or external-use evidence. The canonical external-evidence ledger therefore
reports zero independent reproductions, independent users, institutional
adopters, and third-party reviews; an opened Issue cannot change those counts.
The next meaningful evidence is a GitHub-only release that binds the exact
CI-built bytes, then genuinely independent reproduction or use. Until those
events exist, they remain goals, not résumé claims. See
[the ledger taxonomy and review rules](EXTERNAL_EVIDENCE.md).
