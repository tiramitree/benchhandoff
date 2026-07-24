# Synthetic interruption-recovery benchmark

The supported reviewer-facing entrypoint is `reproduce.py`. From a clean
checkout it creates one commit-bound package containing this focused record,
the 12-task comparison, a bounded summary, a SHA-256 manifest, and a completion
record written last. See [`docs/REPRODUCING.md`](../../docs/REPRODUCING.md) for
the one-command interface and claim boundary. The lower-level scripts below
remain diagnostics.
`worker.py` reads the runner-provided attempt number. Attempt one writes a
partial declared output and terminates immediately with exit code `75`.
BenchHandoff must leave the run failed and omit `bundle.json`.

`run_benchmark.py` then calls `resume` on the same evidence directory. A passing
run requires:

- the first partial output to be a hashed quarantine artifact;
- a second child attempt to complete;
- the final result to be a verified regular file;
- the final bundle to include both attempts' logs and the quarantine artifact;
  and
- a fresh `verify` invocation through the same implementation.

Elapsed times are raw wall-clock observations for that invocation. They are not
presented as a comparison, production measurement, or independent result.
