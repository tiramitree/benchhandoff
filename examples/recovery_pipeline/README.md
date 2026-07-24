# Recovery pipeline example

This synthetic three-task suite demonstrates the intended failure-to-resume
workflow without making timing or model-quality claims. The first `start`
completes feature preparation, then `evaluate.py` deliberately writes a partial
output and exits `17`. BenchHandoff exits `20` and does not create a bundle.
`resume` preserves the completed prefix, quarantines the partial output, retries
the failed task, runs the final task, and creates a verifiable bundle.

Run it from a copied directory so the checked-in example remains unchanged:

```bash
cp -R examples/recovery_pipeline demo-recovery
mkdir -p runs
benchhandoff start demo-recovery/suite.toml --run-dir runs/recovery || test $? -eq 20
benchhandoff resume runs/recovery
benchhandoff verify runs/recovery
```

Inspect `runs/recovery/state.json`, `runs/recovery/quarantine/`, and
`runs/recovery/bundle.json`. This is deterministic local demonstration
evidence, not a production or third-party benchmark.
