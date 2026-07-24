# Clean-room Record

## Scope

BenchHandoff v0.1 was authored in a new directory from a high-level behavioral
brief: sequential `start` / `resume` / `verify` commands, TOML configuration,
regular-file boundaries, content identities, atomic state, logs, fail-closed
child execution, evidence bundling, and a synthetic recovery benchmark.

## Excluded sources

During this implementation:

- no private research source code, schema, path inventory, authorization
  record, or internal implementation document was opened or copied;
- an earlier local resume-state prototype was not opened, imported, or copied;
  and
- no private code was used as a template.

Separate local career-program strategy documents were consulted only for
product positioning and intellectual-property boundaries. They were not
treated as public sources or as implementation references. Interfaces, record
shapes, function names, tests, comments, and implementation details in this
directory were written anew.

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
