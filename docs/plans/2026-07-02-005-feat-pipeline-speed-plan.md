---
title: Pipeline Speed - Plan
type: feat
date: 2026-07-02
topic: pipeline-speed
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Pipeline Speed - Plan

## Goal Capsule

Three targeted backend optimizations to cut pipeline latency from ~30 minutes to under 10 minutes: parallel TTS segment synthesis, batched LLM translation calls, and audio-only YouTube download. Each optimization is independent and can ship separately.

- **Product authority:** STRATEGY.md — 「管道效率」track, "从 30 分钟压到 10 分钟以内"
- **Open blockers:** none

## Product Contract

### Summary

1. **TTS 并发合成** — Replace the sequential `for` loop in `synthesizer.py` with a `ThreadPoolExecutor` of 3-5 workers, synthesizing multiple text segments in parallel.
2. **翻译批量调用** — Merge 3-5 adjacent text segments into a single LLM API request, reducing N round-trips to N/3.
3. **音频流下载** — Add `format: bestaudio` to yt-dlp options so the extractor downloads ~50MB audio instead of ~400MB video+audio.

### Problem Frame

A typical 15-minute YouTube video takes ~25-30 minutes end-to-end. The dominant bottlenecks: TTS consumes 60-70% of total time (sequential API calls per segment), translation consumes 15-20% (N round-trips to DeepSeek), and download wastes 2-3 minutes pulling 400MB of video data that is immediately discarded after audio extraction.

### Key Decisions

**K1. TTS concurrency = 3 workers.** Mimi API has no documented rate limit; 3 concurrent connections balance speed vs. connection overhead. Edge TTS (free fallback) is not concurrently limited either. User-configurable via config.yaml with a sensible default.

**K2. Translation batch = 3-5 segments per call.** DeepSeek's 64K context window can easily hold 3-5 segments plus system prompt. The prompt instructs the model to return translated segments separated by a delimiter, which the client splits back into individual segments. Batch size is capped so no single request exceeds ~12K input tokens.

**K3. Audio-only format selector with fallback.** Primary: `-f bestaudio/best` to prefer audio-only streams. Fallback: if no audio-only format exists (rare edge case), fall back to the current behavior (download full video + extract audio). This is a one-line yt-dlp option change.

### Requirements

**TTS Concurrency**

R1. `src/tts/synthesizer.py` accepts an optional `concurrency: int` parameter (default 3). When >1, segments are dispatched to a `ThreadPoolExecutor` with `max_workers=concurrency`.

R2. Concurrent synthesis preserves segment ordering: each worker writes to `segment_{index:04d}.wav` so the merge stage receives segments in correct order regardless of which worker finishes first.

R3. On any worker failure, the entire synthesis fails with the segment error — same behavior as the current sequential path. Partial results in `tts_segments/` are left on disk for debugging.

**Translation Batching**

R4. `src/translation/client.py` accepts an optional `batch_size: int` parameter (default 3). When >1, segments are grouped into batches of `batch_size` before API calls.

R5. The batched prompt instructs the LLM to output translated segments separated by `---SEGMENT---`. The client splits the response on this delimiter and validates that the segment count matches the input count. On mismatch, it falls back to sequential single-segment calls for that batch.

R6. Batching is transparent to the orchestrator — `llm_translate()` still returns a single combined Chinese text. The batching is an internal optimization of the translation client.

**Audio-Only Download**

R7. `src/youtube/extractor.py` adds `format: bestaudio/best` to the default yt-dlp options so the preferred download is an audio-only stream.

R8. If the audio-only format fails (e.g., geo-restricted, format unavailable), yt-dlp's `best` fallback in the format string automatically tries the next best format that includes audio. No explicit error handling needed — yt-dlp's format negotiation handles it.

### Key Flows

**F1. TTS with concurrency=3**
```
TTS stage starts with 39 segments
  → ThreadPoolExecutor(max_workers=3) created
  → Worker 1: segment_0000.wav | Worker 2: segment_0001.wav | Worker 3: segment_0002.wav
  → As each finishes, next segment dispatched
  → Total: ~39/3 = 13 sequential rounds instead of 39 → ~3x speedup
```

**F2. Translation with batch_size=3**
```
Translation stage starts with 15 text segments
  → Batch 1: segments 0-2 → 1 API call
  → Batch 2: segments 3-5 → 1 API call
  → ...
  → Total: 5 API calls instead of 15 → ~3x fewer round-trips
```

**F3. Audio-only download**
```
yt-dlp invoked with -f bestaudio/best
  → YouTube returns audio-only m4a stream (~50MB)
  → Download completes in ~30s instead of ~2min
  → Extractor still produces .wav via ffmpeg as before
```

### Scope Boundaries

In scope:
- TTS parallel synthesis (ThreadPoolExecutor, configurable concurrency)
- Translation segment batching (prompt-based, delimiter-separated output)
- Audio-only yt-dlp format selector

Out of scope:
- Streaming translation → TTS overlap (#5 from ideation)
- Whisper model preload (#4 from ideation)
- Translation cache (#7 from ideation)
- Frontend changes (these are pure backend optimizations)

### Success Criteria

1. A 15-minute video that previously took 25-30 minutes now completes in under 12 minutes.
2. TTS stage is at least 2× faster with 3 concurrent workers (measured by stage duration in the timeline).
3. YouTube download stage transfers <100MB instead of >300MB for a typical 1080p video.
4. Translated output quality is indistinguishable from sequential single-segment translation.

---

## Planning Contract

### Key Technical Decisions

**KTD1. TTS concurrency via `ThreadPoolExecutor`, not asyncio.** The TTS module is synchronous (HTTP calls via `urllib`). `ThreadPoolExecutor` is the simplest path to I/O parallelism — each worker blocks on its HTTP call independently. The existing `_synthesize_segment` function already has a `ThreadPoolExecutor` for timeout handling; the new executor wraps the segment loop, not individual API calls.

**KTD2. Translation batch delimiter: `---SEGMENT---`.** A unique ASCII delimiter unlikely to appear in translated Chinese text. The system prompt instructs the LLM to separate outputs with this exact string. The client splits on the delimiter and validates segment count. On count mismatch, fall back to sequential single-segment API calls for that batch only — the rest of the batches proceed normally.

**KTD3. yt-dlp format string: `bestaudio/best`.** This prefers an audio-only stream when available, falling back to any stream containing audio if no dedicated audio format exists. yt-dlp's built-in format negotiation handles edge cases (geo-restrictions, missing formats) — no custom fallback logic needed.

**KTD4. Units are independent and can ship in any order.** U1 (TTS), U2 (translation), and U3 (download) touch different files with no shared state. Each is independently verifiable via a pipeline run measuring the relevant stage duration.

### Implementation Units

### U1. TTS concurrent synthesis

**Goal:** Replace the sequential segment loop in `synthesizer.py` with a `ThreadPoolExecutor`, synthesizing up to N segments in parallel.

**Requirements:** R1, R2, R3

**Dependencies:** none

**Files:**
- `src/tts/synthesizer.py` — wrap the segment loop with `ThreadPoolExecutor`

**Approach:**
1. Accept `concurrency: int = 3` parameter in `synthesize()`
2. When `concurrency > 1`: create `ThreadPoolExecutor(max_workers=concurrency)`, submit all segments as futures mapped to their index, collect results in index order
3. Each worker calls the existing `_synthesize_segment(text, output_path, tts_config)` — no change to the per-segment logic
4. On any future raising an exception, cancel remaining futures, log the failed segment index, and re-raise — same fail-fast behavior as sequential
5. The existing `ThreadPoolExecutor` inside `_synthesize_segment` (for timeout) is nested — standard Python supports this; the outer executor manages segments, the inner enforces per-segment timeouts

**Patterns to follow:**
- `concurrent.futures.ThreadPoolExecutor` at `synthesizer.py:133` — already imported
- `future.result(timeout=...)` at `synthesizer.py:161` — existing timeout pattern

**Test scenarios:**
- concurrency=3 with 9 segments → all 9 segments produced, correctly ordered by filename index
- concurrency=3 with 0 segments (empty text) → returns empty list
- concurrency=3 with 1 segment → same output as sequential (no parallelism overhead issue)
- One segment API call fails → entire synthesis fails, error includes segment index
- concurrency=1 → behaves identically to old sequential code (regression safety)

**Verification:** Run a pipeline and observe the TTS stage in the timeline shows reduced duration. With 3 workers, a 39-segment synthesis should be about 3× faster than before.

### U2. Translation batch calls

**Goal:** Group adjacent translation segments into batched API calls, reducing the number of round-trips to the LLM.

**Requirements:** R4, R5, R6

**Dependencies:** none

**Files:**
- `src/translation/client.py` — modify `translate()` to accept and use `batch_size`

**Approach:**
1. Accept `batch_size: int = 1` parameter in `translate()` (default 1 = no batching, backward compatible)
2. When `batch_size > 1`: group `segments` list into chunks of `batch_size`
3. For each batch, construct a prompt: `"将以下 {n} 段英文翻译为中文。每段翻译结果之间用 ---SEGMENT--- 分隔。\n\n" + segments joined with "\n\n"`
4. Parse response: split on `---SEGMENT---`, strip whitespace, validate count
5. On count mismatch: log warning, fall back to sequential single-segment calls for this batch's segments
6. Rejoin all translated segments into the single string the orchestrator expects

**Patterns to follow:**
- `_resolve_llm()` at `client.py:35-44` — existing LLM client resolution
- `chat.completions.create()` call at `client.py:126` — existing API call pattern

**Test scenarios:**
- batch_size=3 with 9 segments → 3 API calls, correct output
- batch_size=5 with 7 segments → 2 calls (5 + 2), correct output
- LLM returns wrong number of delimiters → fallback to sequential for that batch, log warning
- batch_size=1 → identical to old behavior (regression safety)
- Single segment input → 1 API call regardless of batch_size

**Verification:** Run a pipeline with batch_size=3. Count LLM API calls in logs — should be ~1/3 of the sequential count. Output text is semantically equivalent to sequential translation.

### U3. Audio-only YouTube download

**Goal:** Configure yt-dlp to prefer audio-only streams, reducing download size from ~400MB to ~50MB.

**Requirements:** R7, R8

**Dependencies:** none

**Files:**
- `src/youtube/extractor.py` — add `format` to default yt-dlp options

**Approach:**
1. Add `"format": "bestaudio/best"` to the default opts dict in the extract function that builds yt-dlp options
2. yt-dlp's format negotiation: `bestaudio` selects the highest-quality audio-only stream; `/best` is the fallback if no audio-only format exists. Both include audio, so the rest of the pipeline (ffmpeg → WAV → Whisper) is unaffected
3. No changes needed to the download, audio extraction, or post-processing logic — yt-dlp handles the format transparently

**Patterns to follow:**
- `_try_ydl()` at `extractor.py:118` — existing yt-dlp invocation with opts dict
- The `opts` dict already specifies `quiet`, `no_warnings`, etc. — `format` is just another key

**Test scenarios:**
- Standard YouTube video with audio-only format available → downloads audio stream (<100MB)
- Video where audio-only format is unavailable → yt-dlp falls back to `best` (contains audio), download succeeds
- Downloaded file can still be converted to WAV via ffmpeg → Whisper transcription works normally
- Multi-part video (>60min) → both parts download as audio-only

**Verification:** Run a pipeline with a known 1080p video. Check the downloaded file size — should be ~50MB instead of ~400MB. The rest of the pipeline (transcribe → translate → TTS → merge) still produces a valid MP3.

---

## Verification Contract

### Test Commands

- Run full pipeline: `python3 -m uvicorn src.web.server:app --host 127.0.0.1 --port 8080`, submit a video
- Measure stage durations in the timeline UI

### Quality Gates

- **TTS speedup:** With concurrency=3, the TTS stage duration is ≤ 50% of the sequential duration
- **Translation correctness:** Batched translation output is semantically equivalent to sequential output for the same input
- **Download size:** Audio-only stream is <100MB for a typical 15-minute 1080p video
- **No regression:** All three optimizations are opt-in (via parameters with safe defaults); sequential behavior is preserved when concurrency=1, batch_size=1, and format selector falls back gracefully

### Edge Cases

- TTS concurrency with very short text (1 segment) → no unnecessary thread overhead
- Translation batching with segment containing the delimiter string → LLM instructed to escape it
- Audio-only format unavailable (rare YouTube edge case) → yt-dlp `/best` fallback handles it

---

## Definition of Done

### Global

- [ ] TTS stage is at least 2× faster with concurrency=3
- [ ] Translation uses fewer API calls with batch_size=3
- [ ] Download pulls audio stream when available
- [ ] All three changes are backward compatible (default parameters preserve old behavior)
- [ ] Full pipeline produces valid output MP3 end-to-end
- [ ] Product Contract unchanged

### Per-Unit

- [ ] U1 — TTS concurrency=3 produces correct ordered segments, fails fast on error
- [ ] U2 — Translation batch_size=3 produces semantically equivalent output with fewer API calls
- [ ] U3 — yt-dlp downloads audio stream, pipeline still produces valid MP3
