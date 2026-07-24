# Twelve-task restart-versus-resume comparison

The primary synthetic comparison is a chain of twelve sequential tasks. Task 6
writes a partial result and exits `75` on its first invocation.

`run_pipeline_comparison.py` executes two real local strategies:

| Strategy | First pass | Recovery pass | Total child calls | Repeated successful work |
|---|---:|---:|---:|---:|
| Naive full restart | Tasks 1–5 succeed, task 6 fails | Tasks 1–12 rerun | 18 | 5 |
| BenchHandoff resume | Tasks 1–5 succeed, task 6 fails | Task 6 and tasks 7–12 run | 13 | 0 |

Both strategies must end with all 12 task outputs and the same final output
hash. The BenchHandoff path must additionally pass `verify` and retain task 6's
partial result in quarantine.

These numbers follow from the fixed scenario and are asserted by the benchmark
and its unit test. They measure child-process work counts only. No wall-clock,
throughput, hardware, production, or third-party benefit is claimed.

Run:

```powershell
$env:PYTHONPATH = "src"
python benchmarks\synthetic\run_pipeline_comparison.py
```

The shorter `run_benchmark.py` remains as a focused one-task recovery diagnostic.
