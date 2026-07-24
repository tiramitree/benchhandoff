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
decision_sha="$(
  benchhandoff inspect runs/recovery |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["decision_sha256"])'
)"
benchhandoff resume runs/recovery --expected-decision-sha256 "$decision_sha"
benchhandoff verify runs/recovery
```

Inspect `runs/recovery/state.json`, `runs/recovery/quarantine/`, and
`runs/recovery/bundle.json`. The explicit decision token makes this example an
approval-gated resume; plain `resume` remains supported. The token is a local
content binding, not a signature or lock. This is deterministic local
demonstration evidence, not a production or third-party benchmark.
