"""ASR 转写模块。封装 faster-whisper，用于无字幕视频的语音转文字。"""

import json
import logging
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)


def transcribe(
    audio_path: Path,
    output_dir: Optional[Path] = None,
) -> list[dict]:
    """将音频文件转写为英文字幕格式。

    使用 faster-whisper 分段转写长音频（5-10 分钟每段）。

    Args:
        audio_path: 音频文件路径 (.wav 或 .m4a)
        output_dir: 输出目录（存放转写结果 JSON）

    Returns:
        转写结果，格式与字幕提取一致:
        [{"start": "hh:mm:ss", "end": "hh:mm:ss", "text": "..."}, ...]
    """
    cfg = get_config()
    model_name = cfg["asr"]["model"]
    device = cfg["asr"]["device"]
    compute_type = cfg["asr"]["compute_type"]

    from faster_whisper import WhisperModel

    logger.info("Loading whisper model: %s on %s", model_name, device)
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=1,
        language="en",
        vad_filter=True,
    )

    detected_lang = info.language
    logger.info("Detected language: %s, probability: %.2f", detected_lang, info.language_probability)

    entries: list[dict] = []
    for segment in segments:
        entries.append({
            "start": _seconds_to_timestamp(segment.start),
            "end": _seconds_to_timestamp(segment.end),
            "text": segment.text.strip(),
        })

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "transcript_raw.json"
        out_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Transcription complete: %d segments", len(entries))
    return entries


def _seconds_to_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
