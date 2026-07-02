"""管道编排器。协调完整的 YouTube → MP3 处理流程。"""

import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from src.config import get_config
from src.storage import db
from src.pipeline.state import TaskStatus, check_stage_file
from src.youtube.extractor import extract as youtube_extract, check_connectivity
from src.transcription.cleaner import clean as clean_text
from src.transcription.transcriber import transcribe as whisper_transcribe
from src.translation.client import translate as llm_translate, summarize as llm_summarize
from src.tts.cleaner import clean_for_tts
from src.tts.synthesizer import synthesize
from src.audio.merger import merge

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """将视频标题转为安全的文件名。"""
    import re
    safe = re.sub(r'[\\/:*?"<>|\s\']+', '_', name)
    safe = safe.strip('_')[:80]
    return safe or "untitled"


def _resolve_title(video_id: str, data_dir: Path) -> str:
    """解析视频标题用于目录命名。

    1. force 重跑：从 DB 已有记录取标题
    2. 首次运行：尝试从 metadata.json 读取
    3. 都没有：回退到 video_id
    """
    episode = db.get_episode(video_id)
    if episode and episode.get("title_original"):
        return _sanitize_filename(episode["title_original"])

    meta_path = data_dir / "metadata.json"
    if meta_path.exists():
        import json
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("title"):
            return _sanitize_filename(meta["title"])

    # 新视频：先快速获取标题
    try:
        from src.youtube.extractor import _try_ydl
        cfg = get_config()
        proxy = cfg.get("network", {}).get("proxy", "")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True, "writesubtitles": False,
            "noplaylist": True,
        }
        url = f"https://www.youtube.com/watch?v={video_id}"
        info = _try_ydl(opts, url, download=False, proxy=proxy)
        title = info.get("title", video_id)
        return _sanitize_filename(title)
    except Exception:
        return video_id  # 兜底


# 用于并发控制的活跃任务集合
_active_tasks: set[str] = set()
_lock = threading.Lock()


def process(
    video_id: str,
    mode: str = "podcast",
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    llm_config: Optional[dict] = None,
    tts_config: Optional[dict] = None,
) -> dict:
    """执行完整的处理管道。

    Args:
        video_id: YouTube video_id 或 URL
        mode: 输出模式 (faithful | podcast | condensed)
        force: 是否强制重新处理
        on_progress: 进度回调 (message: str)
        llm_config: 动态 LLM 配置 {"api_key": "...", "base_url": "...", "model": "..."}
        tts_config: 动态 TTS 配置 {"provider": "edge"|"openai", "voice": "...", "api_key": "..."}

    Returns:
        {"status": "done"|"failed", "video_id": str, "output_path": str|None, "message": str}
    """
    # 并发防护
    with _lock:
        if video_id in _active_tasks:
            return {"status": "failed", "video_id": video_id, "output_path": None, "message": "该视频正在处理中"}
        _active_tasks.add(video_id)

    try:
        return _process_impl(video_id, mode, force, on_progress, llm_config, tts_config)
    finally:
        with _lock:
            _active_tasks.discard(video_id)


def _process_impl(
    video_id: str,
    mode: str,
    force: bool,
    on_progress: Optional[Callable[[str], None]],
    llm_config: Optional[dict] = None,
    tts_config: Optional[dict] = None,
) -> dict:
    cfg = get_config()
    root = cfg["_project_root"]

    def progress(msg: str):
        logger.info("[%s] %s", video_id, msg)
        if on_progress:
            on_progress(msg)

    # 确保数据库已初始化
    db.init_db()

    # --- 连通性预检 ---
    ok, detail = check_connectivity()
    if not ok:
        progress(f"❌ 网络不通: {detail}")
        return {"status": "failed", "video_id": video_id, "output_path": None, "message": detail}

    # --- 重复检测 ---
    if not force:
        existing = db.is_duplicate(video_id)
        if existing:
            output_path = existing.get("audio_zh_path", "")
            progress("已存在完成记录，跳过处理")
            return {
                "status": "done",
                "video_id": video_id,
                "output_path": output_path,
                "message": "已缓存，跳过处理",
            }

    existing_episode = db.get_episode(video_id)
    stored_data_dir = existing_episode.get("data_dir") if existing_episode else None
    stored_path = Path(stored_data_dir) if stored_data_dir else None

    if stored_path and stored_path.exists():
        # 已有任务的工作目录是持久化标识。标题清理规则变化时不能重新计算目录，
        # 否则 DB 文件路径和磁盘文件会被拆到两个位置。
        data_dir = stored_path
    else:
        # 新任务先用 video_id 做临时路径，解析标题后再决定最终路径。
        tmp_dir = root / cfg["app"]["data_dir"] / video_id
        safe_title = _resolve_title(video_id, tmp_dir)
        data_dir = root / cfg["app"]["data_dir"] / safe_title

        # 如果旧目录在 video_id 名下，迁移到标题名。
        if tmp_dir.exists() and data_dir != tmp_dir:
            try:
                if not data_dir.exists():
                    tmp_dir.rename(data_dir)
                else:
                    import shutil as _shutil
                    for f in tmp_dir.glob("*"):
                        dest = data_dir / f.name
                        if not dest.exists():
                            f.rename(dest)
                    try:
                        tmp_dir.rmdir()
                    except OSError:
                        pass
            except OSError:
                data_dir = tmp_dir

    # force 模式：只清理翻译阶段之后的产物，保留下载 + 转写
    if force and data_dir.exists():
        import shutil
        for f in list(data_dir.glob("*")):
            name = f.name
            if name in ("script_zh.txt", "summary.json", "tts_text.txt", "_concat_list.txt"):
                f.unlink()
            elif name.endswith(".mp3"):
                f.unlink()
            elif name == "tts_segments" and f.is_dir():
                shutil.rmtree(f)
        # 重置 DB 状态为翻译前
        episode = db.get_episode(video_id)
        if episode:
            db.update_status(video_id, "text_ready",
                script_zh_path=None, summary_path=None, audio_zh_path=None, error_message=None)

    data_dir.mkdir(parents=True, exist_ok=True)

    # --- 初始化或加载记录 ---
    episode = db.get_episode(video_id)
    if episode is None:
        db.create_episode(
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            mode=mode,
            data_dir=str(data_dir),
        )
        current_status = TaskStatus.NEW
    else:
        # 从文件系统推断当前状态
        current_status = TaskStatus.NEW
        for status in reversed([s for s in TaskStatus if s not in (TaskStatus.NEW, TaskStatus.FAILED, TaskStatus.DONE)]):
            if check_stage_file(data_dir, status):
                current_status = status
                break

    try:
        # --- Stage: metadata ---
        if _should_run(current_status, TaskStatus.METADATA_FETCHED):
            progress("获取视频信息...")
            try:
                result = youtube_extract(video_id, data_dir)
                db.update_status(
                    video_id,
                    TaskStatus.METADATA_FETCHED.value,
                    title_original=result.meta.title,
                    channel_name=result.meta.channel,
                    duration_seconds=result.meta.duration_seconds,
                    publish_date=result.meta.publish_date,
                    metadata_path=str(data_dir / "metadata.json"),
                )
                current_status = TaskStatus.METADATA_FETCHED
                progress(f"视频: {result.meta.title} ({result.meta.duration_seconds // 60} 分钟)")
            except Exception as e:
                progress(f"获取视频信息失败: {e}")
                raise

        # --- Stage: text ---
        if _should_run(current_status, TaskStatus.TEXT_READY):
            episode = db.get_episode(video_id)
            captions_path_value = episode.get("captions_path") if episode else None
            captions_file = Path(captions_path_value) if captions_path_value else None
            if captions_file and captions_file.is_file():
                progress("开始语音转写...")
                progress("加载已有字幕...")
                captions = json.loads(captions_file.read_text(encoding="utf-8"))
                raw_text = "\n".join(c.get("text", "") for c in captions)
                progress("转写完成")
            else:
                if captions_file:
                    logger.warning("Stored captions file is missing, falling back: %s", captions_file)
                # 检查是否有音频文件需要转写
                audio_files = list(data_dir.glob("*.wav")) + list(data_dir.glob("*.m4a"))
                if audio_files:
                    progress("开始语音转写...")
                    captions = whisper_transcribe(audio_files[0], data_dir)
                    raw_text = "\n".join(c.get("text", "") for c in captions)
                    captions_path = str(data_dir / "captions_en.json")
                    db.update_status(video_id, TaskStatus.TEXT_READY.value, captions_path=captions_path)
                    progress("转写完成")
                else:
                    # 尝试重新提取
                    progress("开始语音转写...")
                    progress("重新获取字幕...")
                    result = youtube_extract(video_id, data_dir)
                    if result.subtitles:
                        raw_text = "\n".join(s.text for s in result.subtitles)
                        progress("转写完成")
                    elif result.audio_path:
                        progress("无字幕，开始语音转写...")
                        captions = whisper_transcribe(result.audio_path, data_dir)
                        raw_text = "\n".join(c.get("text", "") for c in captions)
                        captions_path = str(data_dir / "captions_en.json")
                        db.update_status(video_id, TaskStatus.TEXT_READY.value, captions_path=captions_path)
                        progress("转写完成")
                    else:
                        raise RuntimeError("无法获取字幕或音频")

            progress("清洗文本...")
            cleaned = clean_text(raw_text)
            clean_path = data_dir / "transcript_clean.txt"
            clean_path.write_text(cleaned, encoding="utf-8")
            db.update_status(
                video_id,
                TaskStatus.TEXT_READY.value,
                transcript_clean_path=str(clean_path),
            )
            current_status = TaskStatus.TEXT_READY
            progress(f"文本清洗完成 ({len(cleaned)} 字符)")

        # --- Stage: translate ---
        if _should_run(current_status, TaskStatus.TRANSLATED):
            episode = db.get_episode(video_id)
            clean_text_content = Path(episode["transcript_clean_path"]).read_text(encoding="utf-8")

            metadata = {
                "title": episode.get("title_original", ""),
                "channel": episode.get("channel_name", ""),
            }

            progress(f"开始翻译（模式: {mode}）...")
            script_zh = llm_translate(clean_text_content, mode, metadata, on_progress=progress, llm_config=llm_config)
            script_path = data_dir / "script_zh.txt"
            script_path.write_text(script_zh, encoding="utf-8")

            progress("生成摘要...")
            try:
                summary = llm_summarize(script_zh, metadata, llm_config=llm_config)
            except Exception:
                summary = {"title_zh": metadata.get("title", ""), "summary": "", "key_points": []}

            summary_path = data_dir / "summary.json"
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

            db.update_status(
                video_id,
                TaskStatus.TRANSLATED.value,
                script_zh_path=str(script_path),
                summary_path=str(summary_path),
            )
            current_status = TaskStatus.TRANSLATED
            progress("翻译完成")

        # --- Stage: TTS ---
        if _should_run(current_status, TaskStatus.TTS_DONE):
            episode = db.get_episode(video_id)
            script_zh = Path(episode["script_zh_path"]).read_text(encoding="utf-8")

            progress("TTS 文本清洗...")
            tts_text = clean_for_tts(script_zh)
            tts_text_path = data_dir / "tts_text.txt"
            tts_text_path.write_text(tts_text, encoding="utf-8")

            tts_dir = data_dir / "tts_segments"
            tts_dir.mkdir(exist_ok=True)

            progress("开始语音合成...")
            segments = synthesize(tts_text, tts_dir, on_progress=progress, tts_config=tts_config)

            progress(f"合并 {len(segments)} 个音频片段...")
            episode = db.get_episode(video_id)
            output_name = _sanitize_filename(episode.get("title_original", video_id) if episode else video_id)
            output_base = data_dir / output_name
            output_files = merge(segments, output_base, on_progress=progress)

            db.update_status(
                video_id,
                TaskStatus.TTS_DONE.value,
                audio_zh_path=str(output_files[0]) if output_files else None,
            )
            current_status = TaskStatus.TTS_DONE
            progress(f"MP3 已生成: {len(output_files)} 个文件")

        # --- Stage: done ---
        db.update_status(video_id, TaskStatus.DONE.value)
        episode = db.get_episode(video_id)
        output_path = episode.get("audio_zh_path", "") if episode else ""

        progress("处理完成！")
        return {
            "status": "done",
            "video_id": video_id,
            "output_path": output_path,
            "message": "处理完成",
        }

    except Exception as e:
        error_msg = str(e)
        logger.exception("Pipeline failed for %s: %s", video_id, error_msg)
        db.update_status(video_id, TaskStatus.FAILED.value, error_message=error_msg)
        progress(f"处理失败: {error_msg}")
        return {
            "status": "failed",
            "video_id": video_id,
            "output_path": None,
            "message": error_msg,
        }


def _should_run(current: TaskStatus, target: TaskStatus) -> bool:
    """判断是否需要执行目标阶段。"""
    order = [
        TaskStatus.NEW,
        TaskStatus.METADATA_FETCHED,
        TaskStatus.TEXT_READY,
        TaskStatus.TRANSLATED,
        TaskStatus.TTS_DONE,
        TaskStatus.DONE,
    ]
    try:
        current_idx = order.index(current)
        target_idx = order.index(target)
        return current_idx < target_idx
    except ValueError:
        return True
