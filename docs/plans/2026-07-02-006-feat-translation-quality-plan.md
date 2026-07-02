---
title: Translation Quality - Plan
type: feat
date: 2026-07-02
topic: translation-quality
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Translation Quality - Plan

## Goal Capsule

Four targeted improvements to raise Chinese translation output from "usable" to "broadcast-grade": richer prompts with few-shot examples, an auto-maintained terminology glossary for consistency, a lightweight post-translation quality gate, and speaker-aware translation for multi-voice content.

- **Product authority:** STRATEGY.md — 「输出质量」track, "翻译不丢关键信息、语音合成自然流畅"
- **Open blockers:** none

## Product Contract

### Summary

1. **Prompt 增强** — Each mode prompt gains 2-3 annotated English→Chinese example pairs demonstrating the desired output style directly, plus stronger role instructions.
2. **术语 Glossary** — A SQLite table stores domain term translations extracted from past outputs. The glossary is injected into the prompt's `{{metadata}}` block so the LLM sees "use these translations" before translating.
3. **质量自检** — After translation completes, one additional LLM call (cheaper model) compares key information points in source vs. translation and returns a pass/warn verdict. Warnings are logged; the pipeline continues.
4. **说话人感知** — The text cleaner detects speaker changes in YouTube subtitle cues and annotates them. Translation prompts instruct the LLM to preserve speaker labels and add natural conversational transitions.

### Problem Frame

The current prompt files are 13-19 lines of pure instruction with no worked examples. The LLM must infer the desired output style from description alone. Domain terms (company names, product names, technical jargon) are translated inconsistently across videos — "retrieval-augmented generation" might appear as three different Chinese translations in three different outputs. There is no feedback loop: a translation with missing critical information silently becomes the final MP3 with no quality signal. Multi-speaker content (interviews, panels) flattens into a single undifferentiated voice, losing the conversational dynamic that makes podcasts engaging.

### Key Decisions

**K1. Few-shot examples live in the prompt files, not code.** Each `prompts/translate_*.txt` file gains an `## Examples` section with 2-3 annotated pairs. The prompt loader reads the full file unchanged. This keeps examples versionable in git alongside the instructions, editable without code changes.

**K2. Glossary is append-only with manual override.** Terms are extracted from `script_zh.txt` after each successful translation. The extraction is a lightweight regex + frequency heuristic (proper nouns, capitalized phrases, technical terms appearing 3+ times). The glossary is injected into `{{metadata}}` as a `术语表:` block. Users can manually edit `glossary.db` to fix bad entries.

**K3. Quality check is non-blocking.** The check runs after translation, compares source key points to output, and logs warnings on mismatch. It never blocks the pipeline — a warning is informational, not a failure. This keeps the pipeline fast while building a quality signal history.

**K4. Speaker detection uses YouTube subtitle cues.** YouTube auto-captions often include speaker labels when available (e.g., ">> Speaker 1:"). The text cleaner preserves or infers these cues and marks segment boundaries at speaker changes. When no cues exist, the feature is a no-op — no hallucinated speakers.

### Requirements

**Prompt Enhancement**

R1. Each `prompts/translate_*.txt` gains a `## Examples` section with 2-3 annotated English→Chinese example pairs. Examples are real YouTube content excerpts (not synthetic), showing the desired output style concretely.

R2. The podcast mode prompt gains stronger role instructions: "你是一位资深的中文播客主持人，擅长将英文访谈改写成自然、有节奏感的中文口播稿。你的听众是通勤中的中国 AI 从业者。"

**Terminology Glossary**

R3. `src/storage/db.py` gains a `glossary` table: `id, term_en, term_zh, source_video_id, created_at`. Terms are unique on `term_en`.

R4. After translation completes successfully, the orchestrator calls a new `_extract_terms()` function that regex-scans `script_zh.txt` for proper nouns and technical terms with frequency ≥ 3, then upserts into the glossary table. Extracted terms are printed in the progress log.

R5. Before translation starts, `_load_glossary()` queries all glossary entries and formats them as a `术语表:\n- term_en → term_zh` block injected into the `{{metadata}}` section of the prompt. The glossary is capped at 30 most recent entries to stay within context limits.

**Quality Self-Check**

R6. After translation completes, `_quality_check(source_text, translated_text, metadata)` makes one additional LLM call: "列出原文中的 5-10 个关键信息点（人名、数字、观点、案例），逐条检查译文中是否保留。输出 JSON: [{point, present: true/false, note}]。" On any `present: false`, log a warning with the missing point.

R7. Quality check uses a cheaper model when available (e.g., DeepSeek lite or reduced max_tokens=512) to minimize cost and latency. If the LLM is unavailable, the check is skipped silently.

**Speaker-Aware Translation**

R8. `src/transcription/cleaner.py` detects speaker cues in subtitle text: lines starting with `>>` or matching `Speaker N:` patterns. When detected, segments are annotated with `[说话人 A]:` / `[说话人 B]:` prefixes.

R9. Translation prompts instruct the LLM to preserve speaker labels and add natural turn-taking transitions ("A 问到…", "B 回应说…") in podcast mode. Faithful mode preserves labels without adding transitions.

### Key Flows

**F1. Glossary lifecycle**
```
Pipeline completes translation → script_zh.txt written
→ _extract_terms() scans for proper nouns → upserts into glossary.db
→ Next pipeline run: _load_glossary() → injects into prompt metadata
→ LLM sees "术语表:\n- retrieval-augmented generation → 检索增强生成"
```

**F2. Quality check flow**
```
Translation complete → _quality_check(原文, 译文, meta)
→ LLM: "逐条检查关键信息点是否保留" → returns JSON
→ All present=true → log "质量检查通过 (7/7)"
→ Any present=false → log "⚠ 质量告警: 缺少关键信息 'Q3 revenue 增长 15%' (2/7 项未通过)"
```

### Scope Boundaries

In scope:
- Prompt enhancement with few-shot examples
- SQLite glossary table + auto-extraction + prompt injection
- Post-translation LLM quality check (non-blocking)
- Speaker cue detection + annotation in text cleaner

Out of scope:
- Manual glossary editing UI (glossary.db is SQLite, editable with any tool)
- Multi-pass "translate then polish" (conflicts with pipeline speed track)
- Grammar post-processing (tactical, deferred)
- Real-time quality scoring dashboard

### Success Criteria

1. The same video translated with and without few-shot prompts shows measurable style improvement (evaluated by spot-checking 3 random segments).
2. A domain term appearing in 3+ videos has the same Chinese translation in all outputs.
3. The quality check catches at least one real information-loss incident within 10 pipeline runs, proving the mechanism works.
4. Multi-speaker interview content produces output with distinguishable speakers instead of a single undifferentiated voice.

---

## Planning Contract

### Key Technical Decisions

**KTD1. Few-shot examples are inline in prompt files, not separate files.** The `## Examples` section within each `.txt` file is the single source of truth. No code change needed to load examples — the existing `_load_prompt()` already reads the full file. This keeps examples version-controlled alongside instructions.

**KTD2. Glossary stored in the existing SQLite DB, not a separate file.** Add a `glossary` table to `src/storage/db.py` alongside the `episode` table. Same connection pattern, same WAL mode. Terms are unique on `term_en` with `INSERT OR REPLACE` for upsert.

**KTD3. Quality check uses a separate LLM call with reduced parameters.** `max_tokens=512`, `temperature=0`, and a system prompt optimized for structured JSON output. The check model can be configured independently from the translation model — default to same model, but users can set a cheaper one.

**KTD4. Speaker detection is regex-only, no ML.** Match patterns like `>> Speaker N:`, `[Speaker Name]`, or double-newline-separated paragraphs with alternating voices. The cleaner function adds `[说话人 A]:` / `[说话人 B]:` prefixes. When no patterns match, the text passes through unchanged.

### Implementation Units

### U1. Prompt enhancement with few-shot examples

**Goal:** Add 2-3 annotated English→Chinese example pairs to each mode prompt file, plus stronger role instructions.

**Requirements:** R1, R2

**Dependencies:** none

**Files:**
- `prompts/translate_faithful.txt` — add examples + enhanced role
- `prompts/translate_podcast.txt` — add examples + enhanced role
- `prompts/translate_condensed.txt` — add examples + enhanced role

**Approach:**
1. Append `## Examples` section to each prompt file with 2-3 realistic YouTube-content excerpts and their ideal Chinese translations
2. Update the role instruction (first line) to be more specific and voice-anchored per K1
3. Keep existing numbered requirements intact — examples supplement, not replace

**Patterns to follow:**
- Existing prompt format — `##` section delimiters, numbered requirements
- `_load_prompt()` at `client.py:22-28` — reads entire file, no code change needed

**Test scenarios:**
- Load enhanced prompt via `_load_prompt()` → returns full text including examples section
- Same video translated with old vs. new prompt → new output shows style improvement in spot-check
- All three mode prompts load without syntax errors

**Verification:** Open each prompt file, verify the examples section exists with 2-3 pairs. Run a translation — the LLM receives the examples in context.

### U2. Terminology glossary table + auto-extraction + prompt injection

**Goal:** Add a `glossary` SQLite table, extract domain terms from completed translations, and inject the glossary into future translation prompts.

**Requirements:** R3, R4, R5

**Dependencies:** none

**Files:**
- `src/storage/db.py` — add `glossary` table creation in `init_db()`, add `upsert_glossary_term()`, `get_glossary()`
- `src/translation/client.py` — add `_extract_terms()` and `_load_glossary()`, call from `translate()`
- `src/pipeline/orchestrator.py` — call `_extract_terms()` after translation completes

**Approach:**
1. `init_db()`: `CREATE TABLE IF NOT EXISTS glossary (term_en TEXT UNIQUE, term_zh TEXT, source_video_id TEXT, created_at TEXT)`
2. `upsert_glossary_term(term_en, term_zh, source_video_id)`: `INSERT OR REPLACE`
3. `get_glossary()`: `SELECT * FROM glossary ORDER BY created_at DESC LIMIT 30`
4. `_extract_terms(script_zh_path)`: regex scan for patterns like quoted text, capitalized phrases, tech terms — extract candidates, count frequency, upsert those with freq ≥ 3
5. `_load_glossary()`: query glossary, format as `术语表:\n- term_en → term_zh`, return string for prompt injection
6. In `translate()`, call `_load_glossary()` and append to `meta_str` before prompt construction
7. In orchestrator, after `script_zh.txt` is written, call `_extract_terms()`

**Patterns to follow:**
- `db.py:28-56` (`init_db`) — existing table creation pattern
- `db.py:59-82` (`create_episode`) — existing insert pattern
- `client.py:22-28` (`_load_prompt`) — existing file-read pattern

**Test scenarios:**
- `upsert_glossary_term('RAG', '检索增强生成', 'abc123')` → row inserted
- Same term with different translation → old row replaced
- `get_glossary()` returns ≤ 30 rows, newest first
- Translation with glossary injected → prompt contains `术语表:` block
- `_extract_terms()` on Chinese text with repeated English term → term upserted

**Verification:** Run two pipelines with the same domain content. Check `glossary.db` — terms from first run appear in second run's prompt. Verify second output uses consistent translations.

### U3. Post-translation quality self-check

**Goal:** After translation completes, run one additional LLM call that checks key information preservation and logs warnings.

**Requirements:** R6, R7

**Dependencies:** none (independent of U2, though both touch `client.py`)

**Files:**
- `src/translation/client.py` — add `_quality_check()` function, call after translation
- `src/pipeline/orchestrator.py` — call `_quality_check()` after translation stage

**Approach:**
1. `_quality_check(source_text, translated_text, metadata, llm_config)`: constructs a short prompt asking the LLM to list 5-10 key information points and check their presence, returning JSON
2. Uses `max_tokens=512`, `temperature=0`, `response_format={"type": "json_object"}` for structured output
3. Parses JSON response, counts `present: true/false`, logs summary
4. On any `present: false`, logs `⚠ 质量告警: 缺少关键信息 '...'  (N/M 项通过)`
5. Non-blocking: returns the verdict dict but never raises an exception
6. If the LLM call fails (timeout, API error), logs a warning and continues

**Patterns to follow:**
- `client.py:126-132` — existing `chat.completions.create()` call pattern
- `_resolve_llm()` — reuse existing LLM resolution

**Test scenarios:**
- Quality check on well-translated text → all or most points `present: true`
- Quality check on deliberately truncated translation → some `present: false`, warning logged
- LLM unavailable → function logs warning, returns None, pipeline continues
- JSON response malformed → caught by try/except, logged as unparseable

**Verification:** Run a pipeline. After translation, check logs for `质量检查通过 (N/M)` or `⚠ 质量告警` messages.

### U4. Speaker-aware text cleaning

**Goal:** Detect speaker cues in subtitle text and annotate segments so the LLM preserves multi-voice structure.

**Requirements:** R8, R9

**Dependencies:** none

**Files:**
- `src/transcription/cleaner.py` — add speaker detection logic
- `prompts/translate_podcast.txt` — add speaker-preservation instruction
- `prompts/translate_faithful.txt` — add speaker-preservation instruction

**Approach:**
1. In `cleaner.py`, add `_detect_speakers(text)` before the main cleaning pass
2. Match patterns: `>> Speaker N:`, `>>`, `[Name]:`, `Speaker N:`
3. When speakers detected, assign labels `[说话人 A]` / `[说话人 B]` and prepend to segments
4. When no speakers detected, text passes through unchanged
5. Pass a `speaker_count` flag through the orchestrator to the translation client, which includes a speaker-awareness instruction in the prompt
6. Podcast mode prompt: add "如果原文标注了说话人，请保留说话人标签，并在话轮之间增加自然的过渡语"
7. Faithful mode prompt: add "如果原文标注了说话人，请保留说话人标签"

**Patterns to follow:**
- `cleaner.py` — existing text-cleaning function, add pre-processing step before main clean
- Prompt files — existing numbered-requirement format

**Test scenarios:**
- Subtitle with `>> Speaker 1:` cues → output annotated with `[说话人 A]:` prefixes
- Subtitle without speaker cues → output unchanged, speaker_count=0
- Translation with speaker annotations → output preserves `[说话人 A]:` labels
- Podcast mode with speakers → output includes turn-taking transitions

**Verification:** Take a known interview video with visible speaker cues in subtitles. Run pipeline in podcast mode. Verify output has distinguishable speakers with transition phrases.

---

## Verification Contract

### Test Commands

- Check glossary: `sqlite3 db/video2listener.db "SELECT * FROM glossary ORDER BY created_at DESC LIMIT 10"`
- Test quality check (manual): run pipeline, check logs for quality verdict

### Quality Gates

- **Prompt loading:** All three prompt files load without errors via `_load_prompt()`
- **Glossary persistence:** Terms survive server restart (SQLite)
- **Quality check non-blocking:** Pipeline completes normally even when quality check LLM call fails
- **Speaker detection no false positives:** Plain monologue text without speaker cues passes through unchanged
- **No regression:** Translation output with enhancements is at least as good as without (spot-check)

### Edge Cases

- Glossary grows beyond 30 entries → older entries naturally age out via `LIMIT 30 ORDER BY created_at DESC`
- Quality check LLM returns non-JSON → caught by try/except, logged as unparseable, pipeline continues
- Speaker detection with 3+ speakers → labels as A/B/C, prompt instructs LLM to handle
- Empty glossary → `_load_glossary()` returns empty string, prompt unchanged

---

## Definition of Done

### Global

- [ ] All three prompt files have few-shot examples and enhanced role instructions
- [ ] Glossary table auto-populates from completed translations
- [ ] Glossary is injected into subsequent translation prompts
- [ ] Quality check runs after each translation and logs pass/warn
- [ ] Speaker cues are detected and annotated when present
- [ ] All existing flows (translate, summarize, batched translation) work without regression
- [ ] Product Contract unchanged

### Per-Unit

- [ ] U1 — each prompt file has 2-3 examples, loads correctly
- [ ] U2 — `glossary.db` has entries after first run; second run uses them
- [ ] U3 — quality check logs appear after translation; failures don't block pipeline
- [ ] U4 — speaker annotations appear in cleaned text when cues are present
