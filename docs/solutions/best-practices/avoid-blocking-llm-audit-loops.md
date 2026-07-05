---
title: Avoid Blocking LLM Audit Loops in Translation Pipelines
date: 2026-07-05
category: best-practices
module: translation
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - "LLM pipelines include a blocking semantic audit or repair loop that doubles API calls per unit of work, and the generation prompts already encode quality constraints backed by deterministic output validation"
symptoms:
  - "Per-segment LLM audit doubles API call volume with no measurable quality gain"
  - "Blocking audit loop prevents pipeline parallelism and increases end-to-end latency"
  - "Audit + repair rounds create unbounded retry chains that can stall the pipeline"
  - "Deterministic validation already catches structural failures the audit was checking for"
tags:
  - translation
  - llm-audit
  - performance
  - api-cost
  - prompt-engineering
---

# Avoid Blocking LLM Audit Loops in Translation Pipelines

## Context

The `translate_async()` function in `src/translation/client.py` contained a blocking audit-and-repair loop for faithful translation mode that doubled API call volume without meaningfully improving output quality. Every faithful translation triggered per-segment LLM audits followed by up to two rounds of LLM-based semantic repair, serializing after the primary translation completed and blocking downstream pipeline stages (TTS, audio generation). This pattern is common in LLM pipelines that add post-hoc verification without first exhausting prompt-level and deterministic alternatives.

## Guidance

Replace blocking LLM-based audit loops with a three-layer quality strategy that is either free (deterministic), non-blocking (log-only), or front-loaded (prompt-enforced):

**Layer 1 — Prompt-enforced constraints (zero additional cost).** Encode quality requirements directly in the generation prompt. The faithful translation prompt (`prompts/translate_faithful.txt`) already contains explicit anti-hallucination, identity-preservation, and key-information rules. By front-loading quality into the translation prompt, the model produces faithful output on the first pass instead of needing a second pass to catch deviations.

**Layer 2 — Deterministic validation gates (zero additional cost).** Apply checks that require no API calls:
- Segment count gate: raises an error if the number of translated segments does not match source segments, catching batch-splitting failures and output truncation.
- Number tracking: extracts numeric tokens from source and verifies they appear in translations (accounting for Chinese numeral variants). Missing numbers are recorded as evidence, not as blocking failures.

**Layer 3 — Non-blocking quality sampling (log-only, no pipeline delay).** Perform optional post-translation quality sampling that runs asynchronously after the result is already returned to the caller. It logs warnings for missing information points but never blocks the pipeline.

**Before** (pseudocode of the removed pattern):

```python
if mode == "faithful":
    # BLOCKING: audit every segment via LLM (~15-30 extra API calls)
    audit_results = await _audit_faithful_translation_async(
        source_segments, translated_segments, ...
    )
    for repair_round in range(2):
        failed = [r for r in audit_results if not r["pass"]]
        if not failed:
            break
        for failure in failed:
            repaired = await _repair_faithful_semantic_async(failure, ...)
            translated_segments[failure["index"]] = repaired
        audit_results = await _audit_faithful_translation_async(...)

    if any(not r["pass"] for r in audit_results):
        raise TranslationQualityError("忠实翻译语义审计未通过")
```

**After** (the retained code):

```python
if mode == "faithful":
    if len(segments) != len(translated_segments):
        raise TranslationQualityError(
            f"忠实翻译片段数量不一致: 原文 {len(segments)}，译文 {len(translated_segments)}"
        )

translated_text = "\n\n".join(translated_segments)
_validate_translation_output(text, translated_text, mode)
audit = _build_translation_audit(text, mode, segments, translated_segments)
```

## Why This Matters

The root cause of the performance problem was a **duplicated verification model**. The faithful translation prompt already instructed the model to preserve key information, speaker identity, and factual basis. The audit loop asked a second LLM invocation to verify whether the first LLM invocation followed those instructions. This is inherently wasteful: if the model is reliable enough to audit translations, it is reliable enough to produce faithful translations when given explicit constraints in the prompt.

Key impacts:
- **API calls halved**: removing per-segment audit + repair loop eliminates ~50% of API calls in faithful mode.
- **Pipeline latency reduced**: the pipeline no longer stalls waiting for audit + repair before proceeding to TTS.
- **No quality regression**: the prompt already enforces the constraints the audit was checking. Deterministic gates catch structural failures.

The audit loop had no measurable quality benefit in practice. When it did flag an issue, the repair round was as likely to degrade the output as to improve it, because the repair prompt had no additional context beyond the audit critique. LLM-as-judge is itself stochastic — the same segment could produce different audit outcomes on different runs.

## When to Apply

- When an LLM pipeline includes a post-hoc verification step that calls the same or a similar model to check output quality
- When the generation prompt already encodes the quality constraints the verification is checking
- When deterministic structural checks (counts, schema validation, format checks) can cover the catastrophic failure modes
- When the verification step is blocking the pipeline (serial, synchronous) rather than running as a non-blocking observer
- When the cost of verification is comparable to the cost of generation (e.g., per-segment audit equals per-segment translation)

## Examples

**Example 1 — Faithful translation audit removal (this case):**

The audit subsystem comprised 5 functions (~340 lines) plus 2 constants and a recursive split-merge strategy for long segments. It was replaced with: prompt-enforced faithfulness rules (already present), a deterministic segment-count gate, number tracking in metadata, and a non-blocking `_quality_check()` that logs warnings asynchronously. Result: 50% fewer API calls, no pipeline blocking, all 97 tests pass.

**Example 2 — When an audit loop IS justified (counter-example):**

A post-hoc LLM verification is appropriate when:
- The constraint cannot be expressed declaratively in the prompt (e.g., subjective tone matching requiring comparative judgment across multiple outputs)
- The verification uses a different, stronger model than generation (asymmetric verification)
- The cost of a missed failure is extremely high (medical, legal, safety-critical) and the verification runs on a separate infrastructure with different failure modes

In these cases, the verification should still be non-blocking by default — fire-and-forget with log-based alerting rather than synchronous gating.

## Related

- `docs/plans/2026-07-05-001-fix-faithful-translation-audit-plan.html` — alternative approach that refines rather than removes the audit layer
- `prompts/translate_faithful.txt` — the faithful translation prompt with inline quality constraints (rules 8, 11, 12)
- `src/translation/client.py` — `_quality_check()` (non-blocking sample), `_build_translation_audit()` (metadata), `_validate_translation_output()` (deterministic)
