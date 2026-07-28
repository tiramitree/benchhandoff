# Closed-world workspace example

This synthetic example demonstrates BenchHandoff schema v3 on a dedicated
workspace tree. It is a workspace-integrity and recovery example, not a
sandbox, container, or hostile-code isolation boundary.

Copy this directory before running it because the task intentionally creates
`workspace/result.txt`.

From the copied directory:

```text
benchhandoff inspect-workspace suite.toml
benchhandoff start suite.toml --run-dir ../closed-world-run
benchhandoff verify ../closed-world-run
```

The committed manifest binds every supported primary stream in `workspace/`
before launch. The task reads `input.txt` and creates the declared
`result.txt`. Raw run records and command-line output can contain local
absolute paths, so review them before sharing.
