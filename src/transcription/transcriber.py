"""ASR 转写模块。支持 whisper 与 faster-whisper，用于无字幕视频的语音转文字。"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)


def transcribe(
    audio_path: Path,
    output_dir: Optional[Path] = None,
    language: Optional[str] = "en",
) -> list[dict]:
    """将音频文件转写为字幕格式。

    根据配置使用 whisper (OpenAI .pt 权重)、faster-whisper 或 whisper.cpp
    (ggml .bin 权重 + Vulkan GPU)。
    language 默认为 'en'，可传入 'zh' 或 None（自动探查）。

    Args:
        audio_path: 音频文件路径 (.wav 或 .m4a)
        output_dir: 输出目录（存放转写结果 JSON）
        language: 语言代码，如 'en', 'zh'，传入 None 时自动检测

    Returns:
        转写结果，格式与字幕提取一致:
        [{"start": "hh:mm:ss", "end": "hh:mm:ss", "text": "..."}, ...]
    """
    cfg = get_config()
    asr_cfg = cfg.get("asr", {})
    provider = asr_cfg.get("provider", "whisper")
    model_name = asr_cfg.get("model", "large-v3-turbo")
    model_path_str = asr_cfg.get("model_path")
    device = asr_cfg.get("device", "cpu")
    compute_type = asr_cfg.get("compute_type", "int8")

    # 确定模型加载目标：若配置了 model_path 且存在对应文件/目录，优先使用路径
    resolved_path: Optional[Path] = None
    if model_path_str:
        expanded = Path(os.path.expanduser(str(model_path_str)))
        if expanded.exists():
            resolved_path = expanded

    # 路由 Provider：
    # 1. 若指定的路径是 .pt 文件，faster-whisper/whisper.cpp 都不支持，强制使用 whisper
    # 2. 若显式指定 provider 为 whisper / openai-whisper，使用 whisper
    # 3. 若 provider 为 whisper-cpp，使用 whisper.cpp（ggml + Vulkan GPU）
    # 4. 若 provider 为 faster-whisper，使用 faster-whisper
    # 5. 默认使用 whisper
    is_pt_file = bool(resolved_path and resolved_path.is_file() and resolved_path.suffix == ".pt")
    if is_pt_file or provider in ("whisper", "openai-whisper"):
        target = str(resolved_path) if resolved_path else model_name
        entries = _transcribe_whisper(audio_path, target, device=device, language=language)
    elif provider in ("whisper-cpp", "whisper_cpp", "whisper.cpp"):
        target = str(resolved_path) if resolved_path else model_name
        entries = _transcribe_whisper_cpp(
            audio_path, target, language=language,
            binary=asr_cfg.get("whisper_cpp_bin"),
            threads=asr_cfg.get("whisper_cpp_threads"),
        )
    elif provider == "faster-whisper":
        target = str(resolved_path) if resolved_path else model_name
        entries = _transcribe_faster_whisper(
            audio_path, target, device=device, compute_type=compute_type, language=language
        )
    else:
        target = str(resolved_path) if resolved_path else model_name
        entries = _transcribe_whisper(audio_path, target, device=device, language=language)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(entries, ensure_ascii=False, indent=2)
        (output_dir / "transcript_raw.json").write_text(content, encoding="utf-8")
        (output_dir / "captions_en.json").write_text(content, encoding="utf-8")
        (output_dir / "captions_zh.json").write_text(content, encoding="utf-8")

    logger.info("Transcription complete: %d segments", len(entries))
    return entries


def _transcribe_whisper(
    audio_path: Path,
    model_target: str,
    device: str = "cpu",
    language: Optional[str] = "en",
) -> list[dict]:
    try:
        import whisper
    except ImportError as err:
        raise RuntimeError(
            "未安装 whisper 模块。请安装 openai-whisper: pip install openai-whisper"
        ) from err

    logger.info("Loading whisper model: %s on %s", model_target, device)
    model = whisper.load_model(model_target, device=device)

    transcribe_kwargs = {
        "verbose": False,
    }
    if language is not None:
        transcribe_kwargs["language"] = language
    if device == "cpu":
        transcribe_kwargs["fp16"] = False

    result = model.transcribe(str(audio_path), **transcribe_kwargs)
    detected_lang = result.get("language", language or "en")
    logger.info("Detected language: %s", detected_lang)

    entries: list[dict] = []
    for segment in result.get("segments", []):
        entries.append({
            "start": _seconds_to_timestamp(segment["start"]),
            "end": _seconds_to_timestamp(segment["end"]),
            "text": segment.get("text", "").strip(),
        })
    return entries


def _transcribe_faster_whisper(
    audio_path: Path,
    model_target: str,
    device: str = "cpu",
    compute_type: str = "int8",
    language: Optional[str] = "en",
) -> list[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as err:
        raise RuntimeError(
            "未安装 faster-whisper 模块。请安装: pip install faster-whisper"
        ) from err

    logger.info("Loading faster-whisper model: %s on %s", model_target, device)
    model = WhisperModel(model_target, device=device, compute_type=compute_type)

    transcribe_kwargs = {
        "beam_size": 1,
        "vad_filter": True,
    }
    if language is not None:
        transcribe_kwargs["language"] = language

    segments, info = model.transcribe(
        str(audio_path),
        **transcribe_kwargs,
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
    return entries


def _transcribe_whisper_cpp(
    audio_path: Path,
    model_target: str,
    language: Optional[str] = "en",
    binary: Optional[str] = None,
    threads: Optional[int] = None,
) -> list[dict]:
    """用 whisper.cpp 命令行转写，走 ggml + Vulkan（AMD/NVIDIA/Intel 通用）。

    whisper-cli 只接受 16kHz 单声道 WAV，先用 ffmpeg 转码；再读取
    ``--output-json`` 生成的 JSON（offsets 为毫秒），映射成与其它 provider
    一致的 ``[{start, end, text}]``。
    """
    exe = binary or shutil.which("whisper-cli") or shutil.which("whisper-cli.exe")
    if not exe:
        raise RuntimeError(
            "未找到 whisper-cli。请编译 whisper.cpp (-DGGML_VULKAN=ON) 并在 "
            "config.yaml 的 asr.whisper_cpp_bin 中指定其完整路径。"
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，whisper.cpp 需要它把音频转成 16kHz 单声道 WAV。")

    model_file = Path(model_target)
    if not model_file.is_file():
        raise RuntimeError(f"whisper.cpp 需要 ggml 格式模型 (.bin)，但找不到: {model_target}")

    with tempfile.TemporaryDirectory(prefix="whisper_cpp_") as tmp:
        tmp_dir = Path(tmp)
        wav_path = tmp_dir / "input.wav"
        subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-i", str(audio_path),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)],
            check=True, capture_output=True,
        )

        out_prefix = tmp_dir / "out"
        cmd = [exe, "-m", str(model_file), "-f", str(wav_path),
               "-oj", "-of", str(out_prefix), "-np"]
        if language:
            cmd += ["-l", language]
        if threads:
            cmd += ["-t", str(int(threads))]

        logger.info("Running whisper.cpp on %s", audio_path)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise RuntimeError(f"whisper-cli 退出码 {proc.returncode}: {detail}")
        # whisper.cpp 把后端信息（ggml_vulkan: Found N Vulkan devices）打在 stderr，留痕便于排查。
        for line in (proc.stderr or "").splitlines():
            lowered = line.lower()
            if "vulkan" in lowered or "backend" in lowered:
                logger.info("whisper.cpp: %s", line.strip())

        json_path = out_prefix.with_suffix(".json")
        if not json_path.is_file():
            raise RuntimeError("whisper-cli 未生成 JSON 结果，无法解析转写内容。")
        data = json.loads(json_path.read_text(encoding="utf-8"))

    entries: list[dict] = []
    for seg in data.get("transcription", []):
        offsets = seg.get("offsets") or {}
        start_ms, end_ms = offsets.get("from"), offsets.get("to")
        if start_ms is None or end_ms is None:
            continue
        entries.append({
            "start": _seconds_to_timestamp(start_ms / 1000.0),
            "end": _seconds_to_timestamp(end_ms / 1000.0),
            "text": (seg.get("text") or "").strip(),
        })
    return entries


def _seconds_to_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
