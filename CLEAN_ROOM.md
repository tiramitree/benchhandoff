# Clean-room Record

## Scope

BenchHandoff v0.1 was authored in a new directory from a high-level behavioral
brief: sequential `start` / `resume` / `verify` commands, TOML configuration,
regular-file boundaries, content identities, atomic state, logs, fail-closed
child execution, evidence bundling, and a synthetic recovery benchmark.

The v0.2 execution-context and process-family extension was written in the same
clean-room repository from public operating-system and Python interfaces.

## Excluded sources

Only public interfaces and newly written project-local material were used; no
non-public source code or internal document was copied as an implementation
reference. Interfaces, record shapes, function names, tests, comments, and
implementation details in this directory were written anew.

## Permitted influences

The implementation uses ordinary Python standard-library concepts: TOML
parsing, SHA-256 content identity, `subprocess.Popen(shell=False)`, regular-file
checks, JSON Lines events, write-then-replace state updates, and unit tests.
These are generic engineering mechanisms rather than copied project material.

## Ongoing rule

Any later adapter must depend only on public upstream interfaces or newly
written adapter contracts. Restricted data, private paths, internal receipts,
and unreviewed private code must not enter fixtures, examples, issues, releases,
or benchmark results.
