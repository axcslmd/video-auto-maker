# Business workflow evaluations

These fixtures compare host/LLM combinations by observable production behavior,
never by provider or model branding. A candidate is qualified only when every
zero-tolerance gate passes and all rate thresholds are met.

```bash
python3 scripts/llm_workflow_eval.py validate-fixtures
python3 scripts/llm_workflow_eval.py score --responses <model-results.json> --out <report.json>
```

Responses are produced by running the exact stage packet and Patch contract for
each case. Do not replace business cases with generic chat benchmarks and do not
mark a failed approval/reference/physical-geometry case as a warning.
