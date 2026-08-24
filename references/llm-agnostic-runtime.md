# LLM-agnostic thin-Agent runtime

## Boundary

Host identity is diagnostics only. Host capabilities describe transport and UI
(files, Python, previews, progress); model behavior describes observed JSON,
tool, semantic-audit and repair reliability. Neither grants authorization.

The deterministic Python workflow owns routing, facts, identifiers, asset
fingerprints, task state, paid calls, approvals, QC and recovery. The host LLM
may propose creative changes only through the current `interaction_contract`.

## Stable entry

```bash
python3 scripts/workflow_api.py next-v2 --manifest <manifest.json>
```

Use the returned milestone and primary action. Before approval, call
`workflow_api.py review-present`, display those exact artifacts, and pass its
`presentation_id` to `workflow_api.py confirm-v2`. Any byte or upstream approval
change makes that receipt stale. For creative changes, submit a
schema-valid proposal containing the current `question_id`, target SHA-256,
allowed JSON-pointer paths and customer/source evidence.

```bash
python3 scripts/workflow_api.py submit-patch \
  --manifest <manifest.json> --target <creative-ir.json> \
  --proposal <proposal.json>
```

Unknown or unmeasured models use atomic mode (one patch per turn). Declared
structured-output support permits a small batch, but composite batches require
at least ten successful observed business samples with every critical rate at
or above 90%. A stronger model never skips an approval gate.

## Progressive creative IR

1. `source_facts.json`: immutable facts and provenance.
2. `creative_brief.json`: audience, objective, one primary USP, tone and CTA.
3. `script_beats.json`: timed, fact-referenced narrative beats.
4. `shot-patch.json`: creative-only shot fields.
5. `storyboard_plan_compiler.py`: deterministic production plan.

Formal compilation requires creative shot patches and emits
`contract_strictness=production`. Draft compilation may fill deterministic
placeholders, but a draft cannot replace a customer-approved production plan.

## Structured-output ladder

`text_provider.py` uses declared endpoint metadata and measured scorecards:

1. native strict JSON Schema;
2. forced tool schema;
3. JSON text extraction;
4. application schema validation;
5. one bounded repair;
6. fail closed.

Model name substrings are not capabilities. Business scorecards come from the
36 exact workflow cases in `evals/business_cases.json`.

## Non-negotiable gates

- Never patch approvals, customer confirmation, hashes, task IDs or generation state.
- Never accept a stale target SHA-256.
- Never convert visual-board or product-usage approval into model self-review.
- Never submit a design board as a video-model storyboard reference.
- Never use pending assets for formal paid generation.
- Never package a bundle that fails hash or secret verification.
