"""Web 服务。FastAPI + 静态 UI，接受 YouTube 链接并返回 MP3。"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from src.config import get_config
from src.storage import db
from src.bot.handler import parse_message, extract_youtube_id
from src.pipeline.orchestrator import process as run_pipeline

logger = logging.getLogger(__name__)

# 服务启动时初始化数据库（幂等）
db.init_db()

app = FastAPI(title="video2listener", version="0.1.0")

MODE_LABELS = {"podcast": "中文播客版", "faithful": "忠实翻译版", "condensed": "精华浓缩版"}

MIMO_PRESET_VOICES = [
    {"id": "冰糖", "name": "冰糖 · 中文女声"},
    {"id": "茉莉", "name": "茉莉 · 中文女声"},
    {"id": "苏打", "name": "苏打 · 中文男声"},
    {"id": "白桦", "name": "白桦 · 中文男声"},
    {"id": "Mia", "name": "Mia · English female"},
    {"id": "Chloe", "name": "Chloe · English female"},
    {"id": "Milo", "name": "Milo · English male"},
    {"id": "Dean", "name": "Dean · English male"},
]

STATIC_DIR = Path(__file__).parent / "static"

# ── 内存任务状态（线程安全） ──────────────────────────────────────────
_task_states: dict[str, dict] = {}
_task_lock = threading.Lock()


def _get_task_state(video_id: str) -> Optional[dict]:
    with _task_lock:
        entry = _task_states.get(video_id)
        return dict(entry) if entry else None


def _set_task_state(video_id: str, **kwargs):
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            entry = {
                "video_id": video_id,
                "status": "queued",
                "mode": "",
                "mode_label": "",
                "progress_messages": [],
                "current_stage": "",
                "output_path": None,
                "error_message": None,
                "started_at": time.time(),
                "completed_at": None,
            }
            _task_states[video_id] = entry
        entry.update(kwargs)
        # 保留已有的 progress_messages 如果有
        if "progress_messages" not in kwargs:
            pass  # keep existing


def _append_progress(video_id: str, msg: str):
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry:
            msgs = entry.setdefault("progress_messages", [])
            msgs.append(msg)
            if len(msgs) > 20:
                entry["progress_messages"] = msgs[-20:]
            entry["current_stage"] = msg


# ── 页面 ──────────────────────────────────────────────────────────────


@app.get("/")
async def serve_ui():
    """返回主界面 HTML。"""
    return FileResponse(STATIC_DIR / "index.html")


# ── API ───────────────────────────────────────────────────────────────


@app.post("/api/models")
async def api_models(request: Request):
    """查询可用 LLM 模型列表。

    请求体: {"api_key": "sk-...", "base_url": "https://api.deepseek.com"}
    返回:   {"models": [{"id": "...", "owned_by": "..."}, ...]}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)

    api_key = (body.get("api_key", "") or "").strip()
    base_url = (body.get("base_url", "") or "").strip().rstrip("/")

    if not api_key:
        return JSONResponse({"error": "请输入 API Key"}, status_code=400)
    if not base_url:
        return JSONResponse({"error": "请输入 Base URL"}, status_code=400)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        models = client.models.list()
        result = [{"id": m.id, "owned_by": getattr(m, "owned_by", "")} for m in models]
        return {"models": result}
    except Exception as e:
        logger.warning("Failed to list models: %s", e)
        return JSONResponse({"error": f"查询模型失败: {e}"}, status_code=502)


@app.post("/api/tts-voices")
async def api_tts_voices(request: Request):
    """返回 MiMo V2.5 TTS 官方预置音色列表。

    MiMo 当前没有独立的音色查询接口，音色由官方文档固定提供，
    不应把 /models 的模型 ID 误当成音色 ID。
    """
    try:
        await request.json()
    except Exception:
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)

    return {"voices": MIMO_PRESET_VOICES}


@app.post("/api/process")
async def api_process(request: Request):
    """提交处理任务。

    请求体: {"url": "<youtube_url>", "mode": "podcast"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)

    url = (body.get("url", "") or "").strip()
    mode = (body.get("mode", "") or "").strip()
    api_key = (body.get("api_key", "") or "").strip()
    base_url = (body.get("base_url", "") or "").strip()
    model_name = (body.get("model", "") or "").strip()
    tts_api_key = (body.get("tts_api_key", "") or "").strip()
    tts_base_url = (body.get("tts_base_url", "") or "").strip()
    tts_model = (body.get("tts_model", "") or "mimo-v2.5-tts").strip()
    tts_voice = (body.get("tts_voice", "") or "苏打").strip()
    force = bool(body.get("force", False))

    if not url:
        return JSONResponse({"error": "请输入 YouTube 链接"}, status_code=400)
    if mode not in MODE_LABELS:
        return JSONResponse({"error": f"不支持的模式: {mode}，可选: {list(MODE_LABELS.keys())}"}, status_code=400)

    # 构建动态 LLM 配置（有 api_key 时生效）
    llm_config = None
    if api_key:
        llm_config = {
            "api_key": api_key,
            "base_url": base_url or "https://api.deepseek.com",
            "model": model_name or "deepseek-chat",
        }

    # 构建动态 TTS 配置
    tts_config = None
    if tts_api_key:
        tts_config = {
            "provider": "mimi",
            "api_key": tts_api_key,
            "base_url": tts_base_url or "https://api.xiaomimimo.com/v1",
            "model": tts_model or "mimo-v2.5-tts",
            "voice": tts_voice or "苏打",
        }
    else:
        tts_config = {"provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"}

    video_id = extract_youtube_id(url)
    if not video_id:
        return JSONResponse({
            "error": "无法识别 YouTube 链接，请检查后重试。\n支持格式: youtube.com/watch?v=xxx / youtu.be/xxx / 纯视频 ID"
        }, status_code=400)

    # 重复提交保护（处理中）
    existing = _get_task_state(video_id)
    if existing and existing.get("status") in ("queued", "processing"):
        return JSONResponse({
            "video_id": video_id,
            "status": existing["status"],
            "message": "该视频正在处理中，请勿重复提交",
        })

    # 已完成记录 → 提醒用户确认
    if not force:
        done_record = db.is_duplicate(video_id)
        if done_record:
            return JSONResponse({
                "video_id": video_id,
                "status": "duplicate",
                "message": "该视频之前已处理过，是否重新生成？",
                "output_path": done_record.get("audio_zh_path", ""),
            })

    mode_label = MODE_LABELS[mode]

    # 初始化任务状态
    now = time.time()
    _set_task_state(
        video_id,
        status="queued",
        mode=mode,
        mode_label=mode_label,
        progress_messages=[],
        current_stage="",
        output_path=None,
        error_message=None,
        started_at=now,
        completed_at=None,
    )

    def _run():
        _set_task_state(video_id, status="processing")

        def progress(msg: str):
            _append_progress(video_id, msg)

        result = run_pipeline(video_id, mode, force=force, on_progress=progress, llm_config=llm_config, tts_config=tts_config)

        if result["status"] == "done" and result["output_path"]:
            _set_task_state(
                video_id,
                status="done",
                output_path=result["output_path"],
                completed_at=time.time(),
            )
        else:
            error_msg = result.get("message", "未知错误")
            _set_task_state(
                video_id,
                status="failed",
                error_message=error_msg,
                completed_at=time.time(),
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {
        "video_id": video_id,
        "status": "queued",
        "mode": mode,
        "mode_label": mode_label,
    }


@app.get("/api/status/{video_id}")
async def api_status(video_id: str):
    """查询任务状态和处理进度。"""
    # 先从内存查
    state = _get_task_state(video_id)
    if state:
        return state

    # 兜底：从数据库恢复（服务重启后）
    episode = db.get_episode(video_id)
    if episode:
        db_status = episode.get("status", "new")
        # 映射数据库状态到前端状态
        status_map = {
            "new": "queued",
            "metadata_fetched": "processing",
            "text_ready": "processing",
            "translated": "processing",
            "tts_done": "processing",
            "done": "done",
            "failed": "failed",
        }
        return {
            "video_id": video_id,
            "status": status_map.get(db_status, "unknown"),
            "mode": episode.get("mode", ""),
            "mode_label": MODE_LABELS.get(episode.get("mode", ""), ""),
            "progress_messages": [],
            "current_stage": db_status,
            "output_path": episode.get("audio_zh_path"),
            "error_message": episode.get("error_message"),
            "started_at": None,
            "completed_at": None,
        }

    return JSONResponse({"error": "任务不存在"}, status_code=404)


@app.get("/api/download/{video_id}")
async def api_download(video_id: str):
    """下载生成的 MP3 文件。"""
    # 先从内存查找 output_path
    state = _get_task_state(video_id)
    output_path = state.get("output_path") if state else None

    # 内存没有则查数据库
    if not output_path:
        episode = db.get_episode(video_id)
        if episode:
            output_path = episode.get("audio_zh_path")

    if not output_path:
        return JSONResponse({"error": "文件尚未生成或任务不存在"}, status_code=404)

    path = Path(output_path)
    if not path.exists():
        return JSONResponse({"error": "文件已被移除"}, status_code=404)

    return FileResponse(
        path=path,
        media_type="audio/mpeg",
        filename=path.name,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
