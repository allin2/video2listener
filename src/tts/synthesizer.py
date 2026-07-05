"""TTS 语音合成模块。封装 Mimi TTS，支持分段合成。"""

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import get_config

logger = logging.getLogger(__name__)


def _split_tts_segments(text: str, max_chars: int = 200) -> list[str]:
    """将相邻短句贪心合并为接近接口上限的 TTS 请求。

    cleaner 会为便于阅读和展示进度而把句子分行，但每一行都单独请求会让
    网络往返时间远大于实际合成时间。这里保留句子顺序和换行停顿，并保证
    每个请求不超过 ``max_chars``。
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    segments: list[str] = []
    current = ""

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        for pos in range(0, len(line), max_chars):
            piece = line[pos:pos + max_chars]
            separator = "\n" if current else ""
            if current and len(current) + len(separator) + len(piece) > max_chars:
                segments.append(current)
                current = piece
            else:
                current = current + separator + piece

    if current:
        segments.append(current)

    return segments


def _segment_cache_manifest(
    segments: list[str],
    tts_config: Optional[dict],
    use_ssml: bool,
    speed: float,
    max_chars: int,
) -> dict:
    """构造不含密钥的缓存指纹；文本、模型或音色变化都会自动失效。"""
    config = tts_config or {}
    encoded_segments = json.dumps(segments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "version": 1,
        "segments_sha256": hashlib.sha256(encoded_segments).hexdigest(),
        "segment_count": len(segments),
        "provider": config.get("provider", "edge"),
        "model": config.get("model", ""),
        "voice": config.get("voice", ""),
        "use_ssml": bool(use_ssml),
        "speed": float(speed),
        "max_chars": int(max_chars),
    }


def _is_valid_cached_segment(path: Path, extension: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if extension == ".wav":
        with path.open("rb") as handle:
            header = handle.read(12)
        return len(header) == 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    return True


def _prepare_segment_cache(
    output_dir: Path,
    segments: list[str],
    extension: str,
    manifest: dict,
) -> dict[int, Path]:
    """返回可复用片段；缓存指纹变化时仅清理当前模式的旧片段。"""
    manifest_path = output_dir / "manifest.json"
    existing_manifest = None
    if manifest_path.is_file():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_manifest = None

    if existing_manifest != manifest:
        for path in output_dir.glob("segment_*.*"):
            if path.is_file():
                path.unlink()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {}

    cached: dict[int, Path] = {}
    for index in range(len(segments)):
        path = output_dir / f"segment_{index:04d}{extension}"
        if _is_valid_cached_segment(path, extension):
            cached[index] = path
    return cached


def synthesize(
    text: str,
    output_dir: Path,
    on_progress: Optional[callable] = None,
    tts_config: Optional[dict] = None,
    concurrency: int = 3,
) -> list[Path]:
    """将中文文本合成为音频片段。

    按句子/短段落逐段调用 TTS API。

    Args:
        text: 清洗后的 TTS 文本（已分句，每行一句）
        output_dir: 片段输出目录
        on_progress: 进度回调
        tts_config: 动态 TTS 配置 {"provider": "edge"|"openai", "voice": "...", "api_key": "..."}
        concurrency: 并发合成线程数（默认 3）。设为 1 使用顺序合成（向后兼容）

    Returns:
        音频片段文件路径列表
    """
    cfg = get_config()
    output_dir.mkdir(parents=True, exist_ok=True)

    tts_cfg = cfg.get("tts", {})
    max_chars = int((tts_config or {}).get("max_chars", tts_cfg.get("max_chars", 200)))
    concurrency = int((tts_config or {}).get("concurrency", tts_cfg.get("concurrency", concurrency)))
    concurrency = max(1, concurrency)

    segments = _split_tts_segments(text, max_chars=max_chars)
    if not segments:
        logger.warning("No text to synthesize")
        return []

    audio_files: list[Path] = []
    total = len(segments)

    provider = (tts_config or {}).get("provider", "edge")
    use_ssml = (tts_config or {}).get("use_ssml", True)
    speed = (tts_config or {}).get("speed", 1.0)
    extension = ".wav" if provider == "mimi" else ".mp3"

    manifest = _segment_cache_manifest(
        segments, tts_config, use_ssml, speed, max_chars,
    )
    cached_results = _prepare_segment_cache(
        output_dir, segments, extension, manifest,
    )
    cached_count = len(cached_results)

    if on_progress:
        suffix = f"，复用 {cached_count}" if cached_count else ""
        on_progress(f"TTS 合成中... ({cached_count}/{total}{suffix})")

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_index: dict = {}
            for i, seg in enumerate(segments):
                out_path = output_dir / f"segment_{i:04d}{extension}"
                if i in cached_results:
                    continue
                future = executor.submit(_synthesize_segment, seg, out_path, tts_config, use_ssml, speed)
                future_to_index[future] = (i, out_path)

            results: dict[int, Path] = dict(cached_results)
            completed_count = cached_count
            try:
                for future in as_completed(future_to_index):
                    i, out_path = future_to_index[future]
                    future.result()
                    results[i] = out_path
                    completed_count += 1
                    if on_progress:
                        on_progress(f"TTS 合成中... ({completed_count}/{total})")
            except Exception as e:
                logger.error("TTS segment %d failed: %s", i, e)
                for f in future_to_index:
                    f.cancel()
                raise RuntimeError(f"TTS 合成失败 (segment {i}): {e}") from e

            audio_files = [results[k] for k in sorted(results)]
    else:
        completed_count = cached_count
        for i, seg in enumerate(segments):
            out_path = output_dir / f"segment_{i:04d}{extension}"

            if i in cached_results:
                audio_files.append(cached_results[i])
                continue

            try:
                _synthesize_segment(seg, out_path, tts_config, use_ssml, speed)
                audio_files.append(out_path)
                completed_count += 1
                if on_progress:
                    on_progress(f"TTS 合成中... ({completed_count}/{total})")
            except Exception as e:
                logger.error("TTS segment %d failed: %s", i, e)
                raise RuntimeError(f"TTS 合成失败 (segment {i}): {e}")

    logger.info("TTS synthesis complete: %d segments", len(audio_files))
    return audio_files


async def synthesize_async(
    text: str,
    output_dir: Path,
    on_progress: Optional[callable] = None,
    tts_config: Optional[dict] = None,
    concurrency: int = 3,
) -> list[Path]:
    """异步包装——将同步 synthesize 卸载到线程池。"""
    return await asyncio.to_thread(synthesize, text, output_dir, on_progress, tts_config, concurrency)


def _synthesize_segment(text: str, output_path: Path, tts_config: Optional[dict] = None,
                        use_ssml: bool = True, speed: float = 1.0) -> None:
    """合成单个文本段为音频。

    根据 tts_config 路由到不同 TTS 引擎：
    - edge: Edge TTS (免费，默认，支持 SSML)
    - openai: OpenAI TTS (使用上方 API Key)
    - mimi: Mimi TTS (默认回退)
    """
    provider = tts_config.get("provider", "edge") if tts_config else "edge"

    if provider == "openai":
        _openai_tts(text, output_path, tts_config)
    elif provider == "mimi":
        _mimi_tts(text, output_path, tts_config, speed=speed)
    elif provider == "edge":
        _edge_tts(text, output_path, use_ssml=use_ssml)
    else:
        try:
            _mimi_tts(text, output_path, tts_config, speed=speed)
        except Exception:
            logger.warning("Mimi TTS failed, falling back to edge-tts", exc_info=True)
            _edge_tts(text, output_path, use_ssml=use_ssml)


def _mimi_tts(text: str, output_path: Path, tts_config: Optional[dict] = None,
              speed: float = 1.0) -> None:
    """调用 MiMo Chat Completions TTS，并解码返回的 Base64 WAV。"""
    api_key = (tts_config or {}).get("api_key", "")
    base_url = (tts_config or {}).get("base_url", "https://api.xiaomimimo.com/v1")
    model = (tts_config or {}).get("model", "mimo-v2.5-tts")
    voice = (tts_config or {}).get("voice", "苏打")

    # MiMo TTS 官方格式：user 消息放风格指令，assistant 消息放朗读文本
    # speed 参数有已知 bug（越大反而越慢），不传
    endpoint = base_url.rstrip("/") + "/chat/completions"

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "user", "content": "语气自然流畅，像播客主持人在娓娓道来"},
            {"role": "assistant", "content": text},
        ],
        "audio": {
            "format": "wav",
            "voice": voice,
        },
    }).encode("utf-8")

    # Mimi 用 api-key 头（非标准 Authorization: Bearer）
    req = urllib.request.Request(endpoint, data=payload, headers={
        "api-key": api_key,
        "Content-Type": "application/json",
    })

    # 每个请求使用独立的无代理 opener。不要修改进程级代理环境变量，
    # 否则并发合成时多个线程会互相覆盖环境状态。
    import time as _time

    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)

    def _call_api():
        """同步 API 调用，网络异常时最多重试一次。"""
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                with opener.open(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"MiMo TTS API 返回 HTTP {exc.code}: {detail}") from exc
            except Exception as exc:
                last_error = exc
                logger.warning("MiMo TTS attempt %d/2 failed: %s", attempt + 1, str(exc)[:150])
                if attempt < 1:
                    _time.sleep(2)
        raise RuntimeError(f"MiMo TTS 网络请求失败，已重试 2 次: {last_error}") from last_error

    # opener.open 自身已有 120 秒网络超时。原先每个片段再创建一个单线程池
    # 不会真正缩短阻塞时间，反而为数百个片段重复创建和销毁线程。
    response_body = _call_api()

    try:
        response_data = json.loads(response_body)
        audio_data = response_data["choices"][0]["message"]["audio"]["data"]
        audio_bytes = base64.b64decode(audio_data, validate=True)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("MiMo TTS 响应中缺少有效的 Base64 音频数据") from exc

    if not audio_bytes:
        raise RuntimeError("MiMo TTS 返回了空音频")
    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        raise RuntimeError("MiMo TTS 返回的音频不是有效 WAV")

    output_path.write_bytes(audio_bytes)
    logger.info("Mimi TTS: model=%s voice=%s -> %s (size=%d)", model, voice, output_path, output_path.stat().st_size)


def _edge_tts(text: str, output_path: Path, use_ssml: bool = True) -> None:
    """Edge TTS 方案（开发/测试用，免费），支持 SSML 增强自然度。"""
    import asyncio
    import edge_tts

    cfg = get_config()
    rate = cfg["tts"].get("rate", 1.0)

    # 自动检测语言：非 ASCII 字符占比高则用中文语音，否则用英文语音
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii > len(text) * 0.3:
        voice = cfg["tts"].get("edge_voice", "zh-CN-XiaoxiaoNeural")
    else:
        voice = cfg["tts"].get("edge_voice_en", "en-US-JennyNeural")

    rate_str = f"{int((rate - 1) * 100):+d}%" if rate != 1.0 else "+0%"

    # SSML 文本预处理：用 break 标签替换换行和长破折号，提升自然度
    tts_input = text
    if use_ssml:
        tts_input = text.replace("——", '<break time="300ms"/>')
        tts_input = tts_input.replace("\n\n", '<break time="800ms"/>')
        tts_input = tts_input.replace("\n", '<break time="400ms"/>')
        tts_input = f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="zh-CN">{tts_input}</speak>'

    async def _run():
        # Edge TTS 走直连，清除代理环境变量（否则会被代理拦截导致 SSL 错误）
        import os as _os
        old_proxies = {k: _os.environ.pop(k, None) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
        try:
            communicate = edge_tts.Communicate(tts_input, voice, rate=rate_str)
            await communicate.save(str(output_path))
        finally:
            for k, v in old_proxies.items():
                if v is not None:
                    _os.environ[k] = v

    asyncio.run(_run())


def _openai_tts(text: str, output_path: Path, tts_config: Optional[dict] = None) -> None:
    """OpenAI TTS 合成。使用与 LLM 相同的 API Key 和 Base URL。"""
    from openai import OpenAI

    api_key = (tts_config or {}).get("api_key", "")
    base_url = (tts_config or {}).get("base_url", "https://api.openai.com/v1")
    voice = (tts_config or {}).get("voice", "alloy")

    # OpenAI TTS endpoint 需要完整的 /v1 路径
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    client = OpenAI(api_key=api_key, base_url=base_url)

    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
    )
    response.stream_to_file(str(output_path))
    logger.info("OpenAI TTS: %s -> %s", voice, output_path)
