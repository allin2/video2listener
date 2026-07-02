"""Web 服务。FastAPI + 静态 UI，接受 YouTube 链接并返回 MP3。"""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

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

# ── SSE 阶段定义 ────────────────────────────────────────────────────────
# 7 个用户可见阶段（与内部 TaskStatus 解耦）
SSE_STAGES = [
    {"stage_id": 1, "name": "下载", "icon": "📥"},
    {"stage_id": 2, "name": "转写", "icon": "🎙️"},
    {"stage_id": 3, "name": "清洗", "icon": "🧹"},
    {"stage_id": 4, "name": "翻译", "icon": "🌐"},
    {"stage_id": 5, "name": "TTS", "icon": "🔊"},
    {"stage_id": 6, "name": "合并", "icon": "🎵"},
]

# 从进度消息检测阶段切换：(消息子串, stage_id, 是否为完成标记)
STAGE_TRIGGERS = [
    ("获取视频信息", 1, False),
    ("视频:", 1, True),  # stage 1 done with meta
    ("开始语音转写", 2, False),
    ("加载已有字幕", 2, False),
    ("重新获取字幕", 2, False),
    ("转写完成", 2, True),
    ("清洗文本", 3, False),
    ("文本清洗完成", 3, True),
    ("开始翻译", 4, False),
    ("翻译完成", 4, True),
    ("TTS 文本清洗", 5, False),
    ("开始语音合成", 5, False),
    ("合并", 6, False),
    ("MP3 已生成", 6, True),
]

STATIC_DIR = Path(__file__).parent / "static"

# ── 内存任务状态（线程安全） ──────────────────────────────────────────
_task_states: dict[str, dict] = {}
_task_lock = threading.Lock()


_SSE_INTERNAL_KEYS = {"sse_queue", "sse_loop", "completed_stages", "stage_started_at", "stage_meta"}


def _get_task_state(video_id: str) -> Optional[dict]:
    """返回不含 SSE 内部字段的任务状态副本。"""
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            return None
        return {k: v for k, v in entry.items() if k not in _SSE_INTERNAL_KEYS}


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
                # SSE fields
                "sse_queue": asyncio.Queue(),
                "sse_loop": None,  # set by SSE endpoint
                "current_stage_id": 0,
                "completed_stages": set(),
                "stage_started_at": {},
                "stage_meta": {},
                # Cancel
                "cancel_event": threading.Event(),
            }
            _task_states[video_id] = entry
        entry.update(kwargs)
        # 保留已有的 progress_messages 如果有
        if "progress_messages" not in kwargs:
            pass  # keep existing


def _emit_sse_event(video_id: str, event_type: str, data: dict):
    """将结构化事件推入任务的 SSE 队列（线程安全）。"""
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            return
        queue = entry.get("sse_queue")
        loop = entry.get("sse_loop")
    if queue is None or loop is None:
        return
    payload = {"event": event_type, "data": data}
    try:
        loop.call_soon_threadsafe(queue.put_nowait, payload)
    except asyncio.QueueFull:
        logger.warning("SSE queue full for %s", video_id)


def _detect_stage(msg: str, entry: dict) -> list[dict]:
    """检测进度消息中的阶段切换，返回需要发射的 SSE 事件列表。"""
    events = []
    now = time.time()

    for fragment, stage_id, is_done in STAGE_TRIGGERS:
        if fragment not in msg:
            continue

        current_sid = entry.get("current_stage_id", 0)
        completed = entry.get("completed_stages", set())
        timers = entry.setdefault("stage_started_at", {})
        meta = entry.setdefault("stage_meta", {})

        # 提取阶段 1 元数据（视频标题 + 时长）
        stage_meta = None
        if stage_id == 1 and "视频:" in msg:
            parts = msg.replace("视频: ", "", 1).strip()
            meta[1] = parts
            stage_meta = parts

        if is_done:
            # 阶段完成
            if stage_id not in completed:
                completed.add(stage_id)
                entry["current_stage_id"] = max(stage_id, current_sid)
                duration_s = int(now - timers.get(stage_id, now)) if stage_id in timers else None
                stage_def = SSE_STAGES[stage_id - 1]
                ev = {
                    "type": "stage_change",
                    "stage_id": stage_id, "name": stage_def["name"], "icon": stage_def["icon"],
                    "status": "done",
                    **({"duration_s": duration_s} if duration_s is not None else {}),
                }
                if stage_meta:
                    ev["meta"] = stage_meta
                elif stage_id in meta:
                    ev["meta"] = meta[stage_id]
                events.append(ev)
        elif stage_id > current_sid or (stage_id == current_sid and stage_id not in completed):
            # 新阶段激活（或重新激活）
            # 隐式标记之前所有未完成的阶段为 done
            for sid in range(1, stage_id):
                if sid not in completed:
                    completed.add(sid)
                    ds = int(now - timers.get(sid, now)) if sid in timers else None
                    sd = SSE_STAGES[sid - 1]
                    ev = {
                        "type": "stage_change",
                        "stage_id": sid, "name": sd["name"], "icon": sd["icon"],
                        "status": "done",
                        **({"duration_s": ds} if ds is not None else {}),
                    }
                    if sid in meta:
                        ev["meta"] = meta[sid]
                    events.append(ev)

            # 激活新阶段
            if stage_id not in completed:
                timers[stage_id] = now
                entry["current_stage_id"] = stage_id
                stage_def = SSE_STAGES[stage_id - 1]
                events.append({
                    "type": "stage_change",
                    "stage_id": stage_id, "name": stage_def["name"], "icon": stage_def["icon"],
                    "status": "active",
                })

        # 阶段子进度（仍在同一阶段内）
        elif stage_id == current_sid and not is_done:
            pct = _extract_progress_pct(msg)
            ev = {
                "type": "stage_progress",
                "stage_id": stage_id, "message": msg,
            }
            if pct is not None:
                ev["pct"] = pct
            events.append(ev)

        break  # 只匹配第一个触发词

    return events


def _extract_progress_pct(msg: str) -> Optional[int]:
    """从进度消息中提取百分比（如果有）。"""
    import re
    m = re.search(r'(\d+)/(\d+)', msg)
    if m:
        return int(float(m.group(1)) / float(m.group(2)) * 100)
    m = re.search(r'(\d+)%', msg)
    if m:
        return int(m.group(1))
    return None


def _derive_stages(entry: dict) -> list[dict]:
    """从任务状态推导 stages 数组（用于轮询降级）。"""
    stages = []
    completed = entry.get("completed_stages", set())
    current_sid = entry.get("current_stage_id", 0)
    timers = entry.get("stage_started_at", {})
    meta = entry.get("stage_meta", {})
    status = entry.get("status", "queued")
    now = time.time()

    for sd in SSE_STAGES:
        sid = sd["stage_id"]
        stage = {"stage_id": sid, "name": sd["name"], "icon": sd["icon"], "status": "pending"}

        if sid in completed:
            stage["status"] = "done"
            if sid in timers:
                stage["duration_s"] = int(now - timers[sid])
            if sid in meta:
                stage["meta"] = meta[sid]
        elif sid == current_sid and status in ("processing", "queued"):
            stage["status"] = "active"
            if sid in timers:
                stage["duration_s"] = int(now - timers[sid])
        elif status == "failed" and sid == current_sid:
            stage["status"] = "failed"
        elif status == "cancelled" and sid >= current_sid:
            stage["status"] = "cancelled"

        stages.append(stage)

    return stages


def _append_progress(video_id: str, msg: str):
    """存储进度消息并检测阶段切换，发射 SSE 事件。"""
    events_to_emit = []

    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            return

        msgs = entry.setdefault("progress_messages", [])
        msgs.append(msg)
        if len(msgs) > 20:
            entry["progress_messages"] = msgs[-20:]
        entry["current_stage"] = msg

        # 阶段检测（在锁内，保证对 entry 可变字段的修改是原子的）
        events_to_emit = _detect_stage(msg, entry)

    # 在锁外发射 SSE 事件（避免长时间持锁）
    for ev in events_to_emit:
        event_type = ev.pop("type")
        _emit_sse_event(video_id, event_type, ev)

    # 非阶段切换消息作为 log 事件
    if not events_to_emit:
        _emit_sse_event(video_id, "log", {"message": msg})


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
    resume_from = (body.get("resume_from", "") or "").strip()

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

        # 获取 cancel_event
        with _task_lock:
            entry = _task_states.get(video_id)
            cancel_event = entry.get("cancel_event") if entry else None

        result = run_pipeline(
            video_id, mode, force=force, on_progress=progress,
            llm_config=llm_config, tts_config=tts_config,
            cancel_event=cancel_event,
            resume_from=resume_from or None,
        )

        if result["status"] == "done" and result["output_path"]:
            _set_task_state(
                video_id,
                status="done",
                output_path=result["output_path"],
                completed_at=time.time(),
            )
            # 发射 pipeline_complete
            episode = db.get_episode(video_id)
            title = episode.get("title_original", video_id) if episode else video_id
            _emit_sse_event(video_id, "pipeline_complete", {
                "video_id": video_id,
                "output_path": result["output_path"],
                "title": title,
            })
        elif result["status"] == "cancelled":
            _set_task_state(
                video_id,
                status="cancelled",
                error_message=result.get("message", "用户取消"),
                completed_at=time.time(),
            )
            # 发射 pipeline_cancelled
            resumable_from = result.get("resumable_from", "translated")
            _emit_sse_event(video_id, "pipeline_cancelled", {
                "video_id": video_id,
                "resumable_from": resumable_from,
            })
        else:
            error_msg = result.get("message", "未知错误")
            _set_task_state(
                video_id,
                status="failed",
                error_message=error_msg,
                completed_at=time.time(),
            )
            # 发射 pipeline_error
            with _task_lock:
                entry = _task_states.get(video_id)
                failed_stage = entry.get("current_stage_id", 0) if entry else 0
            _emit_sse_event(video_id, "pipeline_error", {
                "stage_id": failed_stage,
                "message": error_msg,
            })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {
        "video_id": video_id,
        "status": "queued",
        "mode": mode,
        "mode_label": mode_label,
    }


@app.post("/api/cancel/{video_id}")
async def api_cancel(video_id: str):
    """取消正在处理的任务。保留下载和转写阶段的产物，清理翻译及之后的产物。"""
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        status = entry.get("status", "")
        if status not in ("queued", "processing"):
            return JSONResponse({"error": "任务未在运行中", "status": status}, status_code=409)
        cancel_event = entry.get("cancel_event")
        if cancel_event and not cancel_event.is_set():
            cancel_event.set()

    return {"video_id": video_id, "status": "cancelling"}


@app.get("/api/status/{video_id}/stream")
async def api_status_stream(video_id: str, request: Request):
    """SSE 实时进度流。在管道处理期间推送 stage_change / stage_progress / log / pipeline_complete / pipeline_error 事件。"""
    queue = None
    with _task_lock:
        entry = _task_states.get(video_id)
        if entry is None:
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        queue = entry.get("sse_queue")
        entry["sse_loop"] = asyncio.get_running_loop()

    if queue is None:
        return JSONResponse({"error": "SSE 不可用"}, status_code=500)

    async def event_generator():
        try:
            while True:
                # 检查客户端是否断开
                if await request.is_disconnected():
                    break

                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield ": heartbeat\n\n"
                    continue

                event_type = payload.get("event", "log")
                data = payload.get("data", {})

                # 格式化 SSE 输出
                out = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                yield out

                # 终态事件后关闭流
                if event_type in ("pipeline_complete", "pipeline_error"):
                    break
        except asyncio.CancelledError:
            pass  # 客户端断开

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/status/{video_id}")
async def api_status(video_id: str):
    """查询任务状态和处理进度（含 stages 数组用于轮询降级）。"""
    # 先从内存查
    state = _get_task_state(video_id)
    if state:
        # 注入 stages 数组（轮询降级用）
        with _task_lock:
            entry = _task_states.get(video_id)
            if entry:
                state["stages"] = _derive_stages(entry)
            else:
                state["stages"] = []
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
            "cancelled": "cancelled",
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
            "stages": [],
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
