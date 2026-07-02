"""TTS 语音合成模块。封装 Mimi TTS，支持分段合成。"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import get_config

logger = logging.getLogger(__name__)


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

    # 按行拆分（TTS cleaner 已按句拆分）
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        logger.warning("No text to synthesize")
        return []

    # 合并短句为段（每段约 100-200 字，对应 30-60 秒音频）
    segments: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) < 200:
            current += line
        else:
            if current:
                segments.append(current)
            current = line
    if current:
        segments.append(current)

    audio_files: list[Path] = []
    total = len(segments)

    provider = (tts_config or {}).get("provider", "edge")
    use_ssml = (tts_config or {}).get("use_ssml", True)
    speed = (tts_config or {}).get("speed", 1.0)
    extension = ".wav" if provider == "mimi" else ".mp3"

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_index: dict = {}
            for i, seg in enumerate(segments):
                out_path = output_dir / f"segment_{i:04d}{extension}"
                future = executor.submit(_synthesize_segment, seg, out_path, tts_config, use_ssml, speed)
                future_to_index[future] = (i, out_path)

            results: dict[int, Path] = {}
            completed_count = 0
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
        for i, seg in enumerate(segments):
            out_path = output_dir / f"segment_{i:04d}{extension}"

            if on_progress:
                on_progress(f"TTS 合成中... ({i + 1}/{total})")

            try:
                _synthesize_segment(seg, out_path, tts_config, use_ssml, speed)
                audio_files.append(out_path)
            except Exception as e:
                logger.error("TTS segment %d failed: %s", i, e)
                raise RuntimeError(f"TTS 合成失败 (segment {i}): {e}")

    logger.info("TTS synthesis complete: %d segments", len(audio_files))
    return audio_files


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

    # Clamp speed to MiMo supported range 0.25-4.0
    speed = max(0.25, min(4.0, speed))

    endpoint = base_url.rstrip("/") + "/chat/completions"

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "assistant", "content": text},
        ],
        "audio": {
            "format": "wav",
            "voice": voice,
            "speed": speed,
        },
    }).encode("utf-8")

    # Mimi 用 api-key 头（非标准 Authorization: Bearer）
    req = urllib.request.Request(endpoint, data=payload, headers={
        "api-key": api_key,
        "Content-Type": "application/json",
    })

    # 直连，不走代理（环境变量 + 显式 ProxyHandler）
    import os as _os
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    old_proxies = {k: _os.environ.pop(k, None) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "no_proxy", "NO_PROXY")}
    # 使用 ProxyHandler({}) 确保 urllib 不走系统代理
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)

    def _call_api():
        """同步 API 调用（在代理清除后的环境中执行）。"""
        for attempt in range(2):
            try:
                with opener.open(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"MiMo TTS API 返回 HTTP {exc.code}: {detail}") from exc
            except Exception as exc:
                logger.warning("MiMo TTS attempt %d/2 failed: %s", attempt + 1, str(exc)[:150])
                if attempt < 1:
                    _time.sleep(2)
        return None

    response_body = None
    last_error = None
    try:
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_call_api)
                response_body = future.result(timeout=300)  # 硬超时 5 分钟
        except FuturesTimeoutError:
            raise RuntimeError("MiMo TTS 请求超时（5 分钟），已放弃")
        if response_body is None:
            raise RuntimeError(f"MiMo TTS 失败，已重试 2 次: {last_error}")
    finally:
        for k, v in old_proxies.items():
            if v is not None:
                _os.environ[k] = v

    try:
        response_data = json.loads(response_body)
        audio_data = response_data["choices"][0]["message"]["audio"]["data"]
        audio_bytes = base64.b64decode(audio_data, validate=True)
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("MiMo TTS 响应中缺少有效的 Base64 音频数据") from exc

    if not audio_bytes:
        raise RuntimeError("MiMo TTS 返回了空音频")

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
