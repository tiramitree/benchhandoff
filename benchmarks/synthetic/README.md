# Synthetic interruption-recovery benchmark

The supported reviewer-facing entrypoint is `reproduce.py`. From a clean
checkout it creates one commit-bound package containing this focused record,
the 12-task comparison, a bounded summary, a SHA-256 manifest, and a completion
record written last. Its `--verify-dir` mode performs bounded, read-only package
verification and can require an independently obtained full commit SHA. See
[`docs/REPRODUCING.md`](../../docs/REPRODUCING.md) for the cross-platform
interface, source-authentication limit, and claim boundary. The lower-level
scripts below remain diagnostics.
`worker.py` reads the runner-provided attempt number. Attempt one writes a
partial declared output and terminates immediately with exit code `75`.
BenchHandoff must leave the run failed and omit `bundle.json`.

`run_benchmark.py` first inspects the failed run, changes the partial output,
and proves the reviewed decision is stale before refreshing it and calling
bound `resume`. A passing run requires:

- stale-decision rejection to leave state, events, quarantine, partial output,
  and attempt count unchanged;
- the refreshed decision SHA-256 to differ from the stale one;
- the first partial output to be a hashed quarantine artifact;
- a second child attempt to complete;
- the final result to be a verified regular file;
- the final bundle to include both attempts' logs and the quarantine artifact;
  and
- a fresh `verify` invocation through the same implementation.

Elapsed times are raw wall-clock observations for that invocation. They are not
presented as a comparison, production measurement, or independent result.
`run_writer_contention.py` is a lower-level two-process diagnostic for the
cooperative writer exclusion boundary. `run_writer_recovery.py` is the paired
orphan-control diagnostic: a second process hard-exits while holding the lock,
two read-only decisions must match, bound recovery must preserve the original
record as a hard-linked tombstone without changing run evidence or attempt
count, and a separate bound resume must complete and verify attempt 2.

Both scripts report deterministic state counts. Neither is elapsed-time,
production, network-filesystem, hostile-writer, safe-child-retry, independent-
reproduction, or adoption evidence.
