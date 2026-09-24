# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Pipeline

The multi-stage processing sequence that transforms a YouTube URL into Chinese MP3 audio. Stages run in order: Download (yt-dlp) → Transcribe (whisper / faster-whisper) → Clean (text normalization) → Translate (LLM, DeepSeek) → TTS (edge-tts / MiMo) → Merge (ffmpeg). Each stage produces intermediate artifacts; later stages can reuse artifacts from earlier runs when the source material is unchanged.

## Translation Mode

One of three output styles that control how English source text is transformed into Chinese:

- **Podcast** (podcast mode): natural oral Chinese in a podcast-host style. Allows linguistic rewriting and transitional phrasing for flow, but forbids factual additions not present in the source. Speaker labels are preserved with natural transitions between turns.
- **Faithful** (faithful mode): strict preservation of original meaning. Every statement must have direct basis in the source. No identity changes, no inferred background. All key information — cases, steps, numbers, qualifiers — must be preserved. Allows natural rephrasing and omission of filler words.
- **Condensed** (condensed mode): compressed to 30–50% of the **source video's duration**, reorganized by topic, ending with a summary. Targets ~20–30 minutes of audio when the source exceeds 60 minutes. The acceptance gate allows ±5 points (±5 minutes for long sources); see `src/audio/budget.py`.

The mode is selected per request; a single episode can have variants in multiple modes.

## Variant

A mode-specific output of an episode. Each variant has its own translated text, TTS audio segments, and final MP3 file, stored under `variants/<mode>/`. Variants of the same episode share the download, transcription, and cleaning stages — only translation and TTS are re-executed per mode.

## Audit

The translation quality metadata produced after each faithful-mode translation. A lightweight, zero-API-cost record containing per-segment word counts, SHA-256 hashes, and number-preservation tracking. Its `quality_status` is derived only from these deterministic checks: `passed`, `degraded` (faithful numeric recall below 80%, non-blocking), or `not_applicable` (non-faithful modes, and Chinese-source faithful which is not translated). The UI calls this an integrity check, never a semantic audit. Distinct from the removed *blocking audit loop* — a prior LLM-based per-segment semantic verification that was eliminated as redundant because the faithful prompt already encodes the same quality constraints inline.
