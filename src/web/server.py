"""Web 服务。FastAPI + 静态 UI，接受 YouTube 链接并返回 MP3。"""

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from src.config import get_config
from src.storage import db
from src.bot.handler import parse_message, extract_youtube_id
from src.inputs import resolve_canonical_video_id
from src.pipeline.orchestrator import process as run_pipeline, _process_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
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

FISH_PRESET_VOICES = [
    {"id": "7f92f8afb8ec43bf81429cc1c9199cb1", "name": "御姐 · 中文女声（Fish Audio）"},
    {"id": "5c353fdb312f4888836a9a5680099ef0", "name": "女大 · 中文女声（Fish Audio）"},
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
    ("TTS 合成中", 5, False),
    ("合并", 6, False),
    ("MP3 已生成", 6, True),
]

STATIC_DIR = Path(__file__).parent / "static"

# ── 内存任务状态（线程安全） ──────────────────────────────────────────
_task_states: dict[tuple[str, str], dict] = {}
_task_lock = threading.Lock()


_SSE_INTERNAL_KEYS = {
    "sse_queue", "sse_loop", "completed_stages", "stage_started_at", "stage_durations", "stage_meta",
    # threading.Event 内含线程锁，不能交给 FastAPI JSON 序列化。
    "cancel_event", "reused_stages",
}


def _task_key(video_id: str, mode: str) -> tuple[str, str]:
    if mode not in MODE_LABELS:
        raise ValueError(f"不支持的模式: {mode}")
    return video_id, mode


def _find_task_key(video_id: str, mode: Optional[str] = None) -> Optional[tuple[str, str]]:
    if mode:
        key = _task_key(video_id, mode)
        return key if key in _task_states else None
    matches = [
        (key, value) for key, value in _task_states.items() if key[0] == video_id
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            item[1].get("status") in ("queued", "processing"),
            item[1].get("started_at") or 0,
        ),
        reverse=True,
    )
    return matches[0][0]


def _get_task_state(video_id: str, mode: Optional[str] = None) -> Optional[dict]:
    """返回不含 SSE 内部字段的任务状态副本。"""
    with _task_lock:
        key = _find_task_key(video_id, mode)
        entry = _task_states.get(key) if key else None
        if entry is None:
            return None
        return {k: v for k, v in entry.items() if k not in _SSE_INTERNAL_KEYS}


def _task_state_snapshot(entry: dict) -> dict:
    """生成可序列化的完整进度快照，供新建立的 SSE 连接补齐历史事件。"""
    snapshot = {k: v for k, v in entry.items() if k not in _SSE_INTERNAL_KEYS}
    snapshot["stages"] = _derive_stages(entry)
    return snapshot


def _set_task_state(video_id: str, mode: str, **kwargs):
    with _task_lock:
        key = _task_key(video_id, mode)
        entry = _task_states.get(key)
        if entry is None:
            entry = {
                "video_id": video_id,
                "status": "queued",
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "progress_messages": [],
                "current_stage": "",
                "output_path": None,
                "error_message": None,
                "started_at": time.time(),
                "completed_at": None,
                # SSE fields
                # 在 SSE 请求所属事件循环中延迟创建，避免队列绑定到提交请求或
                # 测试创建的另一个事件循环。连接前的进度由状态快照补齐。
                "sse_queue": None,
                "sse_loop": None,  # set by SSE endpoint
                "current_stage_id": 0,
                "completed_stages": set(),
                "stage_started_at": {},
                "stage_durations": {},
                "stage_meta": {},
                "reused_stages": set(),
                # Cancel
                "cancel_event": threading.Event(),
            }
            _task_states[key] = entry
        entry.update(kwargs)
        # 保留已有的 progress_messages 如果有
        if "progress_messages" not in kwargs:
            pass  # keep existing


def _emit_sse_event(video_id: str, mode: str, event_type: str, data: dict):
    """将结构化事件推入任务的 SSE 队列（线程安全）。"""
    with _task_lock:
        entry = _task_states.get(_task_key(video_id, mode))
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

    if msg.startswith("♻️ 复用缓存："):
        stage_name = msg.split("：", 1)[1]
        stage_id = {"下载": 1, "转写": 2, "清洗": 3}.get(stage_name)
        if stage_id:
            entry.setdefault("completed_stages", set()).add(stage_id)
            entry.setdefault("reused_stages", set()).add(stage_id)
            entry.setdefault("stage_meta", {})[stage_id] = "复用缓存"
            entry["current_stage_id"] = max(entry.get("current_stage_id", 0), stage_id)
            stage_def = SSE_STAGES[stage_id - 1]
            return [{
                "type": "stage_change", "stage_id": stage_id,
                "name": stage_def["name"], "icon": stage_def["icon"],
                "status": "reused", "meta": "复用缓存",
            }]

    for fragment, stage_id, is_done in STAGE_TRIGGERS:
        if fragment not in msg:
            continue

        current_sid = entry.get("current_stage_id", 0)
        completed = entry.get("completed_stages", set())
        timers = entry.setdefault("stage_started_at", {})
        durations = entry.setdefault("stage_durations", {})
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
                if duration_s is not None:
                    durations[stage_id] = duration_s
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
        elif stage_id > current_sid:
            # 新阶段激活。同一阶段的后续消息应进入 stage_progress，不能重复激活。
            # 隐式标记之前所有未完成的阶段为 done
            for sid in range(1, stage_id):
                if sid not in completed:
                    completed.add(sid)
                    ds = int(now - timers.get(sid, now)) if sid in timers else None
                    if ds is not None:
                        durations[sid] = ds
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
            meta[stage_id] = msg
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
    reused = entry.get("reused_stages", set())
    current_sid = entry.get("current_stage_id", 0)
    timers = entry.get("stage_started_at", {})
    durations = entry.get("stage_durations", {})
    meta = entry.get("stage_meta", {})
    status = entry.get("status", "queued")
    now = time.time()

    for sd in SSE_STAGES:
        sid = sd["stage_id"]
        stage = {"stage_id": sid, "name": sd["name"], "icon": sd["icon"], "status": "pending"}

        if sid in completed:
            stage["status"] = "reused" if sid in reused else "done"
            if sid in durations:
                stage["duration_s"] = durations[sid]
            if sid in meta:
                stage["meta"] = meta[sid]
        elif sid == current_sid and status in ("processing", "queued"):
            stage["status"] = "active"
            if sid in timers:
                stage["duration_s"] = int(now - timers[sid])
            if sid in meta:
                stage["meta"] = meta[sid]
        elif status == "failed" and sid == current_sid:
            stage["status"] = "failed"
        elif status == "cancelled" and sid >= current_sid:
            stage["status"] = "cancelled"

        stages.append(stage)

    return stages


def _append_progress(video_id: str, mode: str, msg):
    """存储进度消息并检测阶段切换，发射 SSE 事件。"""
    if isinstance(msg, dict) and msg.get("type") == "translation_phase":
        with _task_lock:
            entry = _task_states.get(_task_key(video_id, mode))
            if entry is None:
                return
            phase = dict(msg)
            entry["translation_phase"] = phase
            current = phase.get("current")
            total = phase.get("total")
            suffix = f" ({current}/{total})" if current is not None and total else ""
            entry.setdefault("stage_meta", {})[4] = f"{phase.get('label', '翻译')}{suffix}"
        _emit_sse_event(video_id, mode, "translation_phase", phase)
        return

    msg = str(msg)
    events_to_emit = []

    with _task_lock:
        entry = _task_states.get(_task_key(video_id, mode))
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
        _emit_sse_event(video_id, mode, event_type, ev)

    # 非阶段切换消息作为 log 事件
    if not events_to_emit:
        _emit_sse_event(video_id, mode, "log", {"message": msg})


def _shared_source_ready(episode: Optional[dict]) -> bool:
    """共享清洗文本存在且非空时，后续模式可以跳过前三阶段。"""
    if not episode:
        return False
    path_value = episode.get("transcript_clean_path")
    if not path_value:
        return False
    path = Path(path_value)
    return path.is_file() and path.stat().st_size > 0


# ── 页面 ──────────────────────────────────────────────────────────────


@app.get("/")
async def serve_ui():
    """返回主界面 HTML。"""
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


class _RevalidatingStaticFiles(StaticFiles):
    """静态资源每次向服务端校验（命中 ETag 返回 304），前端更新后刷新即生效。

    不带 Cache-Control 时浏览器会按 Last-Modified 启发式缓存，改了 JS 仍跑旧版。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# 挂载静态资源目录（CSS/JS/图等），供 index.html 引用。
app.mount("/static", _RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/tasks/{video_id}/{mode}/text")
async def api_variant_text(video_id: str, mode: str, kind: str = "script"):
    """读取变体目录下的文本产物（kind=script 中文脚本 / kind=summary 摘要）。

    纯只读端点，不改变任何任务状态。
    """
    if mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    if kind not in ("script", "summary"):
        return JSONResponse({"error": "kind 仅支持 script 或 summary"}, status_code=400)

    variant = db.get_variant(video_id, mode)
    if not variant:
        return JSONResponse({"error": "模式变体不存在"}, status_code=404)

    variant_dir_value = variant.get("variant_dir")
    if not variant_dir_value:
        return JSONResponse({"error": "变体目录不存在"}, status_code=404)
    variant_dir = Path(variant_dir_value)
    if not variant_dir.is_dir():
        return JSONResponse({"error": "变体目录不存在"}, status_code=404)

    if kind == "summary":
        path = variant_dir / "summary.json"
        if not path.is_file():
            return JSONResponse({"error": "摘要尚未生成"}, status_code=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return JSONResponse({"error": "摘要文件解析失败"}, status_code=500)
        lines = []
        if data.get("title_zh"):
            lines.append(data["title_zh"])
        if data.get("summary"):
            lines.append(data["summary"])
        for point in data.get("key_points") or []:
            lines.append(f"- {point}")
        content = "\n\n".join(lines) if lines else "（暂无摘要内容）"
        return Response(content=content, media_type="text/plain; charset=utf-8")

    path = variant_dir / "script_zh.txt"
    if not path.is_file():
        return JSONResponse({"error": "中文脚本尚未生成"}, status_code=404)
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
    )


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
    """返回 TTS 预置音色列表（支持 Fish Audio 与 MiMo）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    provider = (body.get("provider") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    if provider == "mimi" or "xiaomimimo" in base_url:
        return {"voices": MIMO_PRESET_VOICES}
    return {"voices": FISH_PRESET_VOICES}


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
    tts_provider = (body.get("tts_provider", "") or "").strip()
    tts_api_key = (body.get("tts_api_key", "") or "").strip()
    tts_base_url = (body.get("tts_base_url", "") or "").strip()
    tts_model = (body.get("tts_model", "") or "").strip()
    tts_voice = (body.get("tts_voice", "") or "").strip()
    tts_speed = body.get("tts_speed")
    action = (body.get("action", "") or "").strip()
    legacy_force = bool(body.get("force", False))
    if legacy_force and not action:
        action = "regenerate_variant"
    resume_from = (body.get("resume_from", "") or "").strip()

    speed = 1.0
    if tts_speed is not None:
        try:
            speed = float(tts_speed)
            speed = max(0.5, min(2.0, speed))
        except (TypeError, ValueError):
            speed = 1.0

    if not url:
        return JSONResponse({"error": "请输入 YouTube 链接"}, status_code=400)
    if mode not in MODE_LABELS:
        return JSONResponse({"error": f"不支持的模式: {mode}，可选: {list(MODE_LABELS.keys())}"}, status_code=400)
    if action not in ("", "create_variant", "regenerate_variant"):
        return JSONResponse({"error": f"不支持的 action: {action}"}, status_code=400)

    # 构建动态 LLM 配置（有 api_key 时生效）
    llm_config = None
    logger.info("API process request: api_key=%s, model=%s, action=%s",
                "***" if api_key else "(empty)", model_name or "(empty)", action or "(preflight)")
    if api_key:
        llm_config = {
            "api_key": api_key,
            "base_url": base_url or "https://api.deepseek.com",
            "model": model_name or "deepseek-chat",
        }

    # 构建动态 TTS 配置
    tts_config = None
    if tts_api_key:
        provider = tts_provider or ("mimi" if "xiaomimimo" in tts_base_url else "fish")
        if provider == "mimi":
            tts_config = {
                "provider": "mimi",
                "api_key": tts_api_key,
                "base_url": tts_base_url or "https://api.xiaomimimo.com/v1",
                "model": tts_model or "mimo-v2.5-tts",
                "voice": tts_voice or "苏打",
                "speed": speed,
            }
        else:
            fish_voice = tts_voice or "7f92f8afb8ec43bf81429cc1c9199cb1"
            if fish_voice in ("苏打", "白桦", "冰糖", "茉莉", "Mia", "Chloe", "Milo", "Dean", "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"):
                fish_voice = "7f92f8afb8ec43bf81429cc1c9199cb1"
            tts_config = {
                "provider": "fish",
                "api_key": tts_api_key,
                "base_url": tts_base_url or "https://api.fish.audio/v1",
                "model": tts_model or "s2.1-pro-free",
                "voice": fish_voice,
                "speed": speed,
            }
    else:
        tts_config = {"provider": "edge", "voice": tts_voice or "zh-CN-XiaoxiaoNeural", "speed": speed}

    try:
        _, video_id, _ = resolve_canonical_video_id(url)
    except Exception:
        video_id = extract_youtube_id(url)

    if not video_id:
        return JSONResponse({
            "error": "无法识别视频链接，请检查后重试。\n支持格式: YouTube、B站 (BV号/b23短链)、抖音、小红书视频链接或 App 分享文案"
        }, status_code=400)

    # 同一视频串行；不同模式状态仍以复合键独立保存。
    existing = _get_task_state(video_id)
    if existing and existing.get("status") in ("queued", "processing"):
        return JSONResponse({
            "video_id": video_id,
            "mode": existing.get("mode"),
            "mode_label": existing.get("mode_label"),
            "status": existing["status"],
            "started_at": existing.get("started_at"),
            "message": "该视频已有模式正在处理中，请完成或取消后再生成其他模式",
        }, status_code=409)

    episode = db.get_episode(video_id)
    variant = db.get_variant(video_id, mode) if episode else None
    completed_variant = db.get_completed_variant(video_id, mode) if variant else None

    # 无 action 的请求只做决策，不把“新模式”误当成“重新生成”。
    if not action:
        if completed_variant:
            return JSONResponse({
                "video_id": video_id,
                "mode": mode,
                "status": "variant_exists",
                "message": f"该视频已生成{MODE_LABELS[mode]}，可直接下载或重新生成此模式。",
                "output_path": completed_variant.get("audio_zh_path", ""),
                "download_url": f"/api/download/{video_id}/{mode}",
            })
        if variant:
            return JSONResponse({
                "video_id": video_id,
                "mode": mode,
                "status": "variant_exists",
                "message": f"{MODE_LABELS[mode]}存在未完成记录，可重新生成此模式。",
                "output_path": "",
                "download_url": "",
            })
        if episode and _shared_source_ready(episode):
            return JSONResponse({
                "video_id": video_id,
                "mode": mode,
                "status": "reuse_available",
                "message": f"已找到该视频的源素材，将复用下载、转写和清洗缓存生成{MODE_LABELS[mode]}。",
                "reused_stages": ["download", "transcribe", "clean"],
                "existing_modes": [item["mode"] for item in db.list_variants(video_id)],
            })

    if action == "create_variant" and completed_variant:
        return JSONResponse({"error": "该模式已存在，请使用重新生成操作"}, status_code=409)
    if action == "regenerate_variant" and variant is None:
        return JSONResponse({"error": "该模式尚不存在，请使用生成新模式操作"}, status_code=409)

    force = action == "regenerate_variant"

    mode_label = MODE_LABELS[mode]

    # 初始化任务状态（新任务：重置 cancel_event 避免旧取消状态残留）
    now = time.time()
    # 清除旧 entry（如果有）以确保 cancel_event/SSE 队列都是全新的
    with _task_lock:
        _task_states.pop(_task_key(video_id, mode), None)
    _set_task_state(
        video_id,
        mode,
        status="queued",
        progress_messages=[],
        current_stage="",
        output_path=None,
        error_message=None,
        started_at=now,
        completed_at=None,
    )

    async def _run_async():
        _set_task_state(video_id, mode, status="processing")

        def progress(msg: str):
            _append_progress(video_id, mode, msg)

        # 获取 cancel_event
        with _task_lock:
            entry = _task_states.get(_task_key(video_id, mode))
            cancel_event = entry.get("cancel_event") if entry else None

        result = await _process_async(
            video_id, mode, force=force, on_progress=progress,
            llm_config=llm_config, tts_config=tts_config,
            cancel_event=cancel_event,
            resume_from=resume_from or None,
        )

        if result["status"] == "done" and result["output_path"]:
            _set_task_state(
                video_id,
                mode,
                status="done",
                output_path=result["output_path"],
                audit_status=result.get("audit_status", ""),
                audit_message=result.get("audit_message", ""),
                completed_at=time.time(),
            )
            # 发射 pipeline_complete
            episode = db.get_episode(video_id)
            title = episode.get("title_original", video_id) if episode else video_id
            _emit_sse_event(video_id, mode, "pipeline_complete", {
                "video_id": video_id,
                "mode": mode,
                "output_path": result["output_path"],
                "title": title,
                "audit_status": result.get("audit_status", ""),
                "audit_message": result.get("audit_message", ""),
            })
        elif result["status"] == "cancelled":
            _set_task_state(
                video_id,
                mode,
                status="cancelled",
                error_message=result.get("message", "用户取消"),
                completed_at=time.time(),
            )
            # 发射 pipeline_cancelled
            resumable_from = result.get("resumable_from", "translated")
            _emit_sse_event(video_id, mode, "pipeline_cancelled", {
                "video_id": video_id,
                "mode": mode,
                "resumable_from": resumable_from,
            })
        else:
            error_msg = result.get("message", "未知错误")
            _set_task_state(
                video_id,
                mode,
                status="failed",
                error_message=error_msg,
                completed_at=time.time(),
            )
            # 发射 pipeline_error
            with _task_lock:
                entry = _task_states.get(_task_key(video_id, mode))
                failed_stage = entry.get("current_stage_id", 0) if entry else 0
            _emit_sse_event(video_id, mode, "pipeline_error", {
                "stage_id": failed_stage,
                "mode": mode,
                "message": error_msg,
            })

    # fire-and-forget: 不阻塞 API 响应
    asyncio.create_task(_run_async())

    return {
        "video_id": video_id,
        "status": "queued",
        "mode": mode,
        "mode_label": mode_label,
    }


@app.post("/api/cancel/{video_id}/{mode}")
@app.post("/api/cancel/{video_id}")
async def api_cancel(video_id: str, mode: Optional[str] = None):
    """取消正在处理的任务。保留下载和转写阶段的产物，清理翻译及之后的产物。"""
    if mode is not None and mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    with _task_lock:
        key = _find_task_key(video_id, mode)
        entry = _task_states.get(key) if key else None
        if entry is None:
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        status = entry.get("status", "")
        if status not in ("queued", "processing"):
            return JSONResponse({"error": "任务未在运行中", "status": status}, status_code=409)
        cancel_event = entry.get("cancel_event")
        if cancel_event and not cancel_event.is_set():
            cancel_event.set()

    return {"video_id": video_id, "mode": entry.get("mode"), "status": "cancelling"}


@app.get("/api/status/{video_id}/{mode}/stream")
@app.get("/api/status/{video_id}/stream")
async def api_status_stream(video_id: str, request: Request, mode: Optional[str] = None):
    """SSE 实时进度流。在管道处理期间推送 stage_change / stage_progress / log / pipeline_complete / pipeline_error 事件。"""
    if mode is not None and mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    queue = None
    snapshot = None
    with _task_lock:
        key = _find_task_key(video_id, mode)
        entry = _task_states.get(key) if key else None
        if entry is None:
            return JSONResponse({"error": "任务不存在"}, status_code=404)
        # 每次连接都使用当前请求事件循环中的新队列；历史状态由 snapshot 补齐。
        queue = asyncio.Queue()
        entry["sse_queue"] = queue
        entry["sse_loop"] = asyncio.get_running_loop()
        # 管道可能在浏览器建立 SSE 前已经发出复用/阶段事件。连接建立时必须先
        # 推送当前完整状态，不能要求前端依赖已经丢失的瞬时事件。
        snapshot = _task_state_snapshot(entry)

    if queue is None:
        return JSONResponse({"error": "SSE 不可用"}, status_code=500)

    async def event_generator():
        try:
            yield f"event: state_snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if snapshot.get("status") in ("done", "failed", "cancelled"):
                return

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


@app.get("/api/status/{video_id}/{mode}")
@app.get("/api/status/{video_id}")
async def api_status(video_id: str, mode: Optional[str] = None):
    """查询任务状态和处理进度（含 stages 数组用于轮询降级）。"""
    if mode is not None and mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    # 先从内存查
    state = _get_task_state(video_id, mode)
    if state:
        # 注入 stages 数组（轮询降级用）
        with _task_lock:
            key = _find_task_key(video_id, mode)
            entry = _task_states.get(key) if key else None
            if entry:
                state["stages"] = _derive_stages(entry)
            else:
                state["stages"] = []
        return state

    # 兜底：从数据库恢复（服务重启后）
    episode = db.get_episode(video_id)
    if episode:
        resolved_mode = mode if mode in MODE_LABELS else episode.get("mode", "podcast")
        variant = db.get_variant(video_id, resolved_mode)
        source_status = episode.get("status", "new")
        db_status = variant.get("status", "new") if variant else source_status
        if source_status == "failed" and db_status == "new":
            db_status = "failed"
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
        # 错误信息只在对应状态本身失败时透出。变体自身没有错误、却回落到共享的
        # episode 行，会让 A 变体的失败污染 B 变体——查询一个已完成的变体会拿到
        # `status: done` 加一条无关的 error_message。
        variant_error = variant.get("error_message") if variant else None
        if variant_error:
            error_message = variant_error
        elif db_status in ("failed", "cancelled"):
            error_message = episode.get("error_message")
        else:
            error_message = None

        return {
            "video_id": video_id,
            "status": status_map.get(db_status, "unknown"),
            "mode": resolved_mode,
            "mode_label": MODE_LABELS.get(resolved_mode, ""),
            "progress_messages": [],
            "current_stage": db_status,
            "output_path": variant.get("audio_zh_path") if variant else episode.get("audio_zh_path"),
            "error_message": error_message,
            "started_at": None,
            "completed_at": None,
            "summary_path": variant.get("summary_path") if variant else episode.get("summary_path"),
            "audit_status": variant.get("audit_status", "") if variant else "",
            "audit_message": variant.get("audit_message", "") if variant else "",
            "stages": [],
        }

    return JSONResponse({"error": "任务不存在"}, status_code=404)


@app.get("/api/tasks")
async def api_tasks():
    """列出所有历史任务（含进行中的内存状态）。"""
    episodes = db.get_all_episodes()
    variants_by_video: dict[str, list[dict]] = {}
    for variant in db.get_all_variants():
        variants_by_video.setdefault(variant["video_id"], []).append(variant)
    result = []

    # 合并内存状态
    with _task_lock:
        live_states = dict(_task_states)  # shallow copy under lock

    for ep in episodes:
        vid = ep["video_id"]
        variants = []
        for variant in variants_by_video.get(vid, []):
            mode = variant["mode"]
            live = live_states.get(_task_key(vid, mode))
            variant_status = live.get("status") if live else variant.get("status", "new")
            audio_path = (
                live.get("output_path") if live and live.get("output_path")
                else variant.get("audio_zh_path") or ""
            )
            scan_dir = variant.get("variant_dir") or (
                str(Path(audio_path).parent) if audio_path else ""
            )
            parts = _detect_parts(scan_dir, audio_path)
            for part in parts:
                part["download_url"] = f"/api/download/{vid}/{mode}?part={quote(part['filename'])}"
            is_done = variant_status == "done" and audio_path
            variants.append({
                "mode": mode,
                "mode_label": MODE_LABELS.get(mode, mode),
                "status": variant_status,
                "output_seconds": round(_audio_seconds(
                    [part["path"] for part in parts] or [audio_path]
                )) if is_done else 0,
                "variant_dir": variant.get("variant_dir") or "",
                "audio_zh_path": audio_path,
                # 已完成的变体可能残留上次失败的报错，不再展示
                "error_message": "" if is_done else variant.get("error_message") or "",
                "audit_status": variant.get("audit_status") or "",
                "audit_message": variant.get("audit_message") or "",
                "updated_at": variant.get("updated_at") or "",
                "download_url": f"/api/download/{vid}/{mode}",
                "parts": parts,
            })

        variants.sort(key=lambda item: item["updated_at"], reverse=True)
        primary = variants[0] if variants else None
        platform = _platform_of(vid)
        entry = {
            "video_id": vid,
            "platform": platform,
            "thumbnail_url": _thumbnail_url(vid, platform),
            "title_original": ep.get("title_original") or "",
            "channel_name": ep.get("channel_name") or "",
            "mode": primary["mode"] if primary else ep.get("mode", ""),
            "mode_label": primary["mode_label"] if primary else MODE_LABELS.get(ep.get("mode", ""), ""),
            "status": primary["status"] if primary else ep.get("status", "new"),
            "duration_seconds": ep.get("duration_seconds") or 0,
            "data_dir": ep.get("data_dir") or "",
            "audio_zh_path": primary["audio_zh_path"] if primary else ep.get("audio_zh_path") or "",
            "updated_at": primary["updated_at"] if primary else ep.get("updated_at") or "",
            "variants": variants,
        }

        # 旧前端兼容：顶层 parts 指向最近更新的变体。
        entry["parts"] = primary["parts"] if primary else []

        result.append(entry)

    return result


_duration_cache: dict[tuple[str, float], float] = {}


def _audio_seconds(paths: list[str]) -> float:
    """产出音频总时长（秒）；按 (路径, 修改时间) 缓存，读不到时返回 0。"""
    from src.audio.merger import _probe_duration

    total = 0.0
    for raw in paths:
        path = Path(raw)
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            continue
        if key not in _duration_cache:
            try:
                _duration_cache[key] = _probe_duration(path)
            except Exception:
                _duration_cache[key] = 0.0
        total += _duration_cache[key]
    return total


def _platform_of(video_id: str) -> str:
    if video_id.startswith("BV"):
        return "bilibili"
    if video_id.startswith("dy_"):
        return "douyin"
    if video_id.startswith("xhs_"):
        return "xiaohongshu"
    return "youtube"


def _thumbnail_url(video_id: str, platform: str) -> str:
    # 只有 YouTube 能由 ID 直接拼出封面地址；其他平台前端显示平台占位图
    if platform == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    return ""


def _part_sort_key(path: Path) -> tuple[int, str]:
    """多集文件的自然排序键：按 Part 序号而非字符串顺序。

    字符串排序会把 episode_Part10.mp3 排在 episode_Part2.mp3 前面，
    导致下载列表集数错乱。
    """
    match = re.search(r"_Part(\d+)", path.stem)
    return (int(match.group(1)) if match else 0, path.name)


def _detect_parts(data_dir: str, primary_path: str) -> list[dict]:
    """检测 data_dir 中的多 Part MP3 文件。返回下载链接列表。"""
    parts = []
    if not data_dir:
        return parts
    d = Path(data_dir)
    if not d.is_dir():
        return parts

    # 扫描 *_Part*.mp3（按序号自然排序，避免 Part10 排在 Part2 之前）
    part_files = sorted(d.glob("*_Part*.mp3"), key=_part_sort_key)
    for pf in part_files:
        parts.append({"filename": pf.name, "path": str(pf)})

    # 主 MP3（如果不是 Part 文件）
    if primary_path:
        pp = Path(primary_path)
        if pp.exists() and pp.suffix == ".mp3" and "Part" not in pp.stem:
            parts.insert(0, {"filename": pp.name, "path": str(pp)})
    elif not parts:
        # 没有 primary_path，扫描目录下普通 mp3
        for mp3 in sorted(d.glob("*.mp3")):
            if "Part" not in mp3.stem:
                parts.append({"filename": mp3.name, "path": str(mp3)})

    return parts


@app.delete("/api/tasks/{video_id}/{mode}")
async def api_delete_variant(video_id: str, mode: str):
    """删除单个模式变体；零拷贝旧变体不会删除共享根目录文件。"""
    if mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    with _task_lock:
        live = _task_states.get(_task_key(video_id, mode))
    if live and live.get("status") in ("queued", "processing"):
        return JSONResponse({"error": "该模式正在处理中，请先取消"}, status_code=409)

    episode = db.get_episode(video_id)
    variant = db.get_variant(video_id, mode) if episode else None
    if not variant:
        return JSONResponse({"error": "模式变体不存在"}, status_code=404)

    variant_dir_value = variant.get("variant_dir")
    if variant_dir_value and episode.get("data_dir"):
        import shutil
        variants_root = (Path(episode["data_dir"]) / "variants").resolve()
        target = Path(variant_dir_value).resolve()
        expected = (variants_root / mode).resolve()
        if target != expected or not target.is_relative_to(variants_root):
            return JSONResponse({"error": "变体目录不安全，已拒绝删除"}, status_code=409)
        if target.is_dir():
            shutil.rmtree(target)

    db.delete_variant(video_id, mode)
    with _task_lock:
        _task_states.pop(_task_key(video_id, mode), None)
    return {"video_id": video_id, "mode": mode, "deleted": True}


@app.delete("/api/tasks/{video_id}")
async def api_delete_task(video_id: str):
    """删除任务记录及 WAV 文件，保留转写/翻译中间产物。"""
    # 拒绝删除进行中的任务
    with _task_lock:
        live = [
            state for (vid, _mode), state in _task_states.items() if vid == video_id
        ]
    if any(state.get("status") in ("queued", "processing") for state in live):
        return JSONResponse(
            {"error": "任务正在处理中，无法删除。请先取消任务"},
            status_code=409,
        )

    episode = db.get_episode(video_id)
    if not episode:
        return JSONResponse({"error": "任务不存在"}, status_code=404)

    data_dir = episode.get("data_dir") or ""
    if data_dir:
        d = Path(data_dir)
        if d.is_dir():
            # 删除所有 WAV 文件
            for wav in d.glob("*.wav"):
                try:
                    wav.unlink()
                    logger.info("Deleted WAV: %s", wav)
                except OSError as e:
                    logger.warning("Failed to delete WAV %s: %s", wav, e)
            variants_root = d / "variants"
            if variants_root.is_dir():
                import shutil
                shutil.rmtree(variants_root)

    db.delete_episode(video_id)
    return {"video_id": video_id, "deleted": True}


@app.post("/api/tts-preview")
async def api_tts_preview(request: Request):
    """合成一句测试短语并返回 WAV 音频，用于 TTS 音色试听。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体须为 JSON"}, status_code=400)

    provider = (body.get("provider") or "").strip()
    voice = (body.get("voice") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    model = (body.get("model") or "").strip()
    raw_speed = body.get("speed")
    if raw_speed is None:
        raw_speed = body.get("tts_speed")
    try:
        speed = float(raw_speed) if raw_speed is not None else 1.0
        speed = max(0.5, min(2.0, speed))
    except (TypeError, ValueError):
        speed = 1.0

    if not provider:
        provider = "fish" if api_key else "edge"

    if provider not in ("edge", "mimi", "fish"):
        return JSONResponse({"error": f"不支持的 TTS provider: {provider}"}, status_code=400)

    # 构建 TTS 配置
    if provider == "fish" and api_key:
        fish_voice = voice or "7f92f8afb8ec43bf81429cc1c9199cb1"
        if fish_voice in ("苏打", "白桦", "冰糖", "茉莉", "Mia", "Chloe", "Milo", "Dean", "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"):
            fish_voice = "7f92f8afb8ec43bf81429cc1c9199cb1"
        tts_config = {
            "provider": "fish",
            "api_key": api_key,
            "base_url": base_url or "https://api.fish.audio/v1",
            "model": model or "s2.1-pro-free",
            "voice": fish_voice,
            "speed": speed,
        }
    elif provider == "mimi" and api_key:
        tts_config = {
            "provider": "mimi",
            "api_key": api_key,
            "base_url": base_url or "https://api.xiaomimimo.com/v1",
            "model": model or "mimo-v2.5-tts",
            "voice": voice or "苏打",
            "speed": speed,
        }
    else:
        tts_config = {"provider": "edge", "voice": voice or "zh-CN-XiaoxiaoNeural", "speed": speed}

    test_phrase = "你好，这是音色试听。"

    tmp_dir = None
    try:
        import tempfile
        import shutil
        from src.tts.synthesizer import synthesize

        tmp_dir = Path(tempfile.mkdtemp(prefix="tts_preview_"))
        segments = synthesize(test_phrase, tmp_dir, tts_config=tts_config)

        if not segments or not segments[0].exists():
            return JSONResponse({"error": "TTS 合成失败，未生成音频"}, status_code=502)

        wav_path = segments[0]

        # FileResponse 发送完成后再清理，避免流式读取期间文件被提前删除。
        def _cleanup():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        suffix = wav_path.suffix.lower()
        media_type = "audio/wav" if suffix == ".wav" else "audio/mpeg"

        return FileResponse(
            path=wav_path,
            media_type=media_type,
            filename=f"tts_preview{suffix}",
            background=BackgroundTask(_cleanup),
        )
    except Exception as e:
        if tmp_dir is not None:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.warning("TTS preview failed: %s", e)
        return JSONResponse({"error": f"TTS 试听失败: {e}"}, status_code=502)


@app.get("/api/download/{video_id}/{mode}")
@app.get("/api/download/{video_id}")
async def api_download(video_id: str, mode: Optional[str] = None, part: Optional[str] = None):
    """下载生成的 MP3 文件。"""
    if mode is not None and mode not in MODE_LABELS:
        return JSONResponse({"error": "不支持的模式"}, status_code=400)
    # 先从内存查找 output_path
    state = _get_task_state(video_id, mode)
    output_path = state.get("output_path") if state else None

    # 内存没有则查数据库
    if not output_path:
        episode = db.get_episode(video_id)
        if episode:
            if mode in MODE_LABELS:
                variant = db.get_completed_variant(video_id, mode)
            else:
                variants = [item for item in db.list_variants(video_id) if item["status"] == "done"]
                variants.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
                variant = variants[0] if variants else None
            output_path = (
                variant.get("audio_zh_path") if variant else episode.get("audio_zh_path")
            )

    if not output_path:
        return JSONResponse({"error": "文件尚未生成或任务不存在"}, status_code=404)

    path = Path(output_path)
    if part:
        if mode not in MODE_LABELS or Path(part).name != part or not part.lower().endswith(".mp3"):
            return JSONResponse({"error": "无效的分段文件"}, status_code=400)
        variant = db.get_variant(video_id, mode)
        scan_dir = Path(variant.get("variant_dir") or path.parent) if variant else path.parent
        candidate = (scan_dir / part).resolve()
        if candidate.parent != scan_dir.resolve():
            return JSONResponse({"error": "无效的分段文件"}, status_code=400)
        path = candidate
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
