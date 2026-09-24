"""管道编排器。协调完整的 YouTube → MP3 处理流程。"""

import asyncio
import json
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import get_config
from src.storage import db
from src.pipeline.state import (
    TaskStatus,
    check_shared_stage_file,
    check_variant_stage_file,
)
from src.inputs import Platform
from src.sources.router import extract as router_extract, check_connectivity as router_check_connectivity

# 保持向后兼容（现有测试直接 monkeypatch orchestrator.youtube_extract / check_connectivity）
youtube_extract = router_extract
check_connectivity = router_check_connectivity
from src.transcription.cleaner import clean as clean_text
from src.transcription.transcriber import transcribe as whisper_transcribe
from src.translation.client import (
    translate as llm_translate,
    translate_async as llm_translate_async,
    summarize as llm_summarize,
    _extract_terms,
    _quality_check,
    _validate_translation_output,
)
from src.tts.cleaner import clean_for_tts
from src.tts.synthesizer import synthesize, synthesize_async
from src.audio.budget import (
    condensed_overshoot,
    duration_budget,
    predict_duration,
    synthesized_duration_error,
)
from src.audio.merger import _probe_duration, merge, merge_async

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """将视频标题转为安全的文件名。"""
    import re
    safe = re.sub(r'[\\/:*?"<>|\s\']+', '_', name)
    safe = safe.strip('_')[:80]
    return safe or "untitled"


def _resolve_title(video_id: str, data_dir: Path) -> str:
    """解析视频标题用于目录命名。

    1. 从 DB 已有记录取标题
    2. 尝试从 metadata.json 读取
    3. 都没有：回退到 API / yt-dlp → video_id
    """
    episode = db.get_episode(video_id)
    if episode and episode.get("title_original"):
        return _sanitize_filename(episode["title_original"])

    meta_path = data_dir / "metadata.json"
    if meta_path.exists():
        import json
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        title = meta.get("title", "")
        if title:
            # 修复 DB 中缺失的标题
            if episode:
                db.update_status(video_id, episode.get("status", "new"), title_original=title)
            return _sanitize_filename(title)

    # 新视频：先快速获取标题
    try:
        if video_id.startswith("BV"):
            from src.sources.bilibili import fetch_view
            view = fetch_view(video_id)
            return _sanitize_filename(view["title"]) if view else video_id
        if video_id.startswith(("dy_", "xhs_")):
            # 非 YouTube ID 不能交给 YouTube 的 yt-dlp（会重试多次后才失败），
            # 标题由各平台适配器在提取阶段写入 metadata.json
            return video_id
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


def _existing_stage_paths(data_dir: Path) -> dict[str, str]:
    """从磁盘恢复数据库中可能缺失的共享阶段文件路径。"""
    candidates = {
        "metadata_path": data_dir / "metadata.json",
        "captions_path": data_dir / "captions_en.json",
        "transcript_clean_path": data_dir / "transcript_clean.txt",
    }
    recovered = {
        key: str(path)
        for key, path in candidates.items()
        if path.is_file() and path.stat().st_size > 0
    }
    return recovered


def _variant_dir(data_dir: Path, mode: str) -> Path:
    """返回模式专属目录，拒绝未知模式。"""
    if mode not in db.VALID_MODES:
        raise ValueError(f"不支持的模式: {mode}")
    return data_dir / "variants" / mode


def _cleanup_variant_outputs(variant_dir: Path, resume_from: Optional[str] = None) -> None:
    """只清理目标模式产物；绝不扫描共享目录或其他模式。"""
    import shutil

    if not variant_dir.is_dir():
        return
    keep_translation = resume_from == "translated"
    for path in list(variant_dir.iterdir()):
        if keep_translation and path.name in (
            "script_zh.txt", "summary.json", "translation_audit.json",
        ):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _new_variant_workspace(target_dir: Path) -> Path:
    """为同模式重新生成创建隔离工作区，避免半成品覆盖已完成产物。"""
    workspace = target_dir.parent / f".{target_dir.name}.regenerating-{uuid.uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


def _commit_variant_workspace(workspace: Path, target_dir: Path) -> None:
    """原子切换同模式目录；切换失败时恢复旧目录。"""
    backup = target_dir.parent / f".{target_dir.name}.backup-{uuid.uuid4().hex}"
    moved_old = False
    try:
        if target_dir.exists():
            os.replace(target_dir, backup)
            moved_old = True
        os.replace(workspace, target_dir)
    except Exception:
        if moved_old and backup.exists() and not target_dir.exists():
            os.replace(backup, target_dir)
        raise
    finally:
        if backup.exists() and target_dir.exists():
            shutil.rmtree(backup)


def _discard_variant_workspace(workspace: Optional[Path], target_dir: Path) -> None:
    if workspace and workspace != target_dir and workspace.exists():
        shutil.rmtree(workspace)


def _path_in_committed_workspace(path: Optional[Path], workspace: Path, target_dir: Path) -> Optional[Path]:
    if path is None:
        return None
    return target_dir / path.relative_to(workspace)


def _ensure_distinct_mode_script(video_id: str, mode: str, script: str) -> None:
    """拒绝把另一个模式的相同脚本再次注册为新模式。"""
    normalised = "".join(script.split())
    for variant in db.list_variants(video_id):
        if variant.get("mode") == mode:
            continue
        path_value = variant.get("script_zh_path")
        if not path_value:
            continue
        path = Path(path_value)
        if not path.is_file():
            continue
        other = "".join(path.read_text(encoding="utf-8").split())
        if normalised and normalised == other:
            raise RuntimeError(
                f"{mode} 模式生成内容与已有 {variant.get('mode')} 模式完全相同；"
                "已停止 TTS，请重新生成该模式。"
            )


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
    cancel_event=None,
    resume_from: Optional[str] = None,
) -> dict:
    """同步兼容包装——内部调用 asyncio.run。"""
    return asyncio.run(_process_async(video_id, mode, force, on_progress, llm_config, tts_config, cancel_event, resume_from))


async def _process_async(
    video_id: str,
    mode: str = "podcast",
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    llm_config: Optional[dict] = None,
    tts_config: Optional[dict] = None,
    cancel_event=None,
    resume_from: Optional[str] = None,
) -> dict:
    """异步执行完整的处理管道。"""
    with _lock:
        if video_id in _active_tasks:
            return {"status": "failed", "video_id": video_id, "output_path": None, "message": "该视频正在处理中"}
        _active_tasks.add(video_id)

    try:
        return await _process_impl(video_id, mode, force, on_progress, llm_config, tts_config, cancel_event, resume_from)
    finally:
        with _lock:
            _active_tasks.discard(video_id)


def _check_cancel(cancel_event, progress) -> bool:
    """检查取消信号。返回 True 表示已取消。
    支持 threading.Event 和 asyncio.Event。
    """
    if cancel_event and cancel_event.is_set():
        progress("⏹ 用户取消，正在清理...")
        return True
    return False


async def _process_impl(
    video_id: str,
    mode: str,
    force: bool,
    on_progress: Optional[Callable[[str], None]],
    llm_config: Optional[dict] = None,
    tts_config: Optional[dict] = None,
    cancel_event=None,
    resume_from: Optional[str] = None,
) -> dict:
    cfg = get_config()
    root = cfg["_project_root"]

    # resume_from 映射到 TaskStatus
    _RESUME_MAP = {
        "metadata_fetched": TaskStatus.METADATA_FETCHED,
        "text_ready": TaskStatus.TEXT_READY,
        "translated": TaskStatus.TRANSLATED,
        "tts_done": TaskStatus.TTS_DONE,
    }

    def progress(msg: str):
        logger.info("[%s] %s", video_id, msg)
        if on_progress:
            on_progress(msg)

    # 确保数据库已初始化
    db.init_db()

    # --- 指定模式重复检测 ---
    if not force:
        existing_variant = db.get_completed_variant(video_id, mode)
        if existing_variant:
            output_path = existing_variant.get("audio_zh_path", "")
            progress(f"已存在完成的 {mode} 模式，跳过处理")
            return {
                "status": "done",
                "video_id": video_id,
                "mode": mode,
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

    data_dir.mkdir(parents=True, exist_ok=True)

    platform = (
        Platform.BILIBILI if video_id.startswith("BV")
        else Platform.DOUYIN if video_id.startswith("dy_")
        else Platform.XIAOHONGSHU if video_id.startswith("xhs_")
        else Platform.YOUTUBE
    )
    if platform == Platform.BILIBILI:
        episode_url = f"https://www.bilibili.com/video/{video_id}"
    elif platform == Platform.DOUYIN:
        episode_url = f"https://www.douyin.com/video/{video_id[3:]}"
    elif platform == Platform.XIAOHONGSHU:
        episode_url = f"https://www.xiaohongshu.com/discovery/item/{video_id[4:]}"
    else:
        episode_url = f"https://www.youtube.com/watch?v={video_id}"

    # --- 初始化或加载共享记录 ---
    episode = db.get_episode(video_id)
    if episode is None:
        db.create_episode(
            video_id=video_id,
            url=episode_url,
            mode=mode,
            data_dir=str(data_dir),
        )
        episode = db.get_episode(video_id)
    else:
        # 服务重启或旧版本可能只留下磁盘文件、没有保存对应 DB 路径。
        # 在判断恢复阶段前补齐路径，确保强制重新生成可以复用已有转写。
        recovered_paths = _existing_stage_paths(data_dir)
        missing_paths = {
            key: value for key, value in recovered_paths.items() if not episode.get(key)
        }
        if missing_paths:
            db.update_status(video_id, episode.get("status", TaskStatus.NEW.value), **missing_paths)
            episode = db.get_episode(video_id)

    shared_status = TaskStatus.NEW
    if check_shared_stage_file(data_dir, TaskStatus.TEXT_READY):
        shared_status = TaskStatus.TEXT_READY
    elif check_shared_stage_file(data_dir, TaskStatus.METADATA_FETCHED):
        shared_status = TaskStatus.METADATA_FETCHED

    final_target_dir = _variant_dir(data_dir, mode)
    existing_variant = db.get_variant(video_id, mode)
    atomic_regeneration = bool(force and existing_variant is not None)
    target_dir = final_target_dir

    # 强制重生成只触碰目标模式。仅明确从 metadata 恢复时才使共享清洗失效。
    if force and not atomic_regeneration:
        _cleanup_variant_outputs(target_dir, resume_from=resume_from)
    if force:
        if resume_from == "metadata_fetched":
            clean_path = data_dir / "transcript_clean.txt"
            if clean_path.exists():
                clean_path.unlink()
            db.update_status(
                video_id, TaskStatus.METADATA_FETCHED.value,
                transcript_clean_path=None, error_message=None,
            )
            shared_status = TaskStatus.METADATA_FETCHED

    if atomic_regeneration:
        target_dir = _new_variant_workspace(final_target_dir)
        # “从 TTS 重试”需要把已验证译文复制进隔离工作区，其余重新生成均从空目录开始。
        if resume_from == "translated":
            for name in ("script_zh.txt", "summary.json", "translation_audit.json"):
                source = final_target_dir / name
                if source.is_file():
                    shutil.copy2(source, target_dir / name)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        variant_updates = {"variant_dir": str(final_target_dir)}
        if force and resume_from != "translated":
            variant_updates.update({
                "script_zh_path": None,
                "summary_path": None,
                "tts_text_path": None,
                "tts_segments_dir": None,
                "audio_zh_path": None,
                "error_message": None,
                "audit_status": None,
                "audit_message": None,
                "translation_audit_path": None,
            })
        elif force:
            variant_updates.update({
                "tts_text_path": None,
                "tts_segments_dir": None,
                "audio_zh_path": None,
                "error_message": None,
            })
        db.upsert_variant(
            video_id,
            mode,
            status="new" if force or existing_variant is None else existing_variant["status"],
            **variant_updates,
        )
    variant = db.get_variant(video_id, mode)

    variant_status = TaskStatus.NEW
    if check_variant_stage_file(target_dir, TaskStatus.TTS_DONE):
        variant_status = TaskStatus.TTS_DONE
    elif check_variant_stage_file(target_dir, TaskStatus.TRANSLATED):
        variant_status = TaskStatus.TRANSLATED
    elif not force and variant:
        script_value = variant.get("script_zh_path")
        script_path = Path(script_value) if script_value else None
        if script_path and script_path.is_file() and script_path.stat().st_size > 0:
            variant_status = TaskStatus.TRANSLATED

    if resume_from and resume_from in _RESUME_MAP:
        resume_status = _RESUME_MAP[resume_from]
        if atomic_regeneration and resume_status == TaskStatus.TRANSLATED:
            script = target_dir / "script_zh.txt"
            if not script.is_file() or script.stat().st_size <= 0:
                resume_status = TaskStatus.NEW
        progress(f"从阶段恢复: {resume_from} ({resume_status.value})")
        if resume_status in (TaskStatus.METADATA_FETCHED, TaskStatus.TEXT_READY):
            shared_status = resume_status
        else:
            variant_status = resume_status

    if shared_status == TaskStatus.TEXT_READY:
        progress("♻️ 复用缓存：下载")
        progress("♻️ 复用缓存：转写")
        progress("♻️ 复用缓存：清洗")

    deferred_summary = None
    audit_status = (
        variant.get("audit_status") if mode == "faithful" and variant else "not_applicable"
    ) or ("pending" if mode == "faithful" else "not_applicable")
    audit_message = variant.get("audit_message", "") if variant else ""
    existing_audit_path = target_dir / "translation_audit.json"
    audit_path: Optional[Path] = existing_audit_path if existing_audit_path.is_file() else None

    try:
        # --- Cancel check before metadata ---
        if _check_cancel(cancel_event, progress):
            _discard_variant_workspace(target_dir, final_target_dir)
            if not atomic_regeneration:
                db.update_status(video_id, TaskStatus.CANCELLED.value, error_message="用户取消")
            return {"status": "cancelled", "video_id": video_id, "mode": mode, "output_path": None,
                    "message": "用户取消", "resumable_from": "new"}

        # --- Stage: metadata ---
        if _should_run(shared_status, TaskStatus.METADATA_FETCHED):
            ok, detail = check_connectivity(platform)
            if not ok:
                progress(f"❌ 网络不通: {detail}")
                raise RuntimeError(detail)
            progress("获取视频信息...")
            try:
                result = await asyncio.to_thread(youtube_extract, video_id, data_dir)
                db.update_status(
                    video_id,
                    TaskStatus.METADATA_FETCHED.value,
                    title_original=result.meta.title,
                    channel_name=result.meta.channel,
                    duration_seconds=result.meta.duration_seconds,
                    publish_date=result.meta.publish_date,
                    metadata_path=str(data_dir / "metadata.json"),
                )
                shared_status = TaskStatus.METADATA_FETCHED
                progress(f"视频: {result.meta.title} ({result.meta.duration_seconds // 60} 分钟)")
            except Exception as e:
                progress(f"获取视频信息失败: {e}")
                raise

        # --- Cancel check before text ---
        if _check_cancel(cancel_event, progress):
            _discard_variant_workspace(target_dir, final_target_dir)
            if not atomic_regeneration:
                db.update_status(video_id, TaskStatus.CANCELLED.value, error_message="用户取消")
            return {"status": "cancelled", "video_id": video_id, "mode": mode, "output_path": None,
                    "message": "用户取消", "resumable_from": "metadata_fetched"}

        # --- Stage: text ---
        if _should_run(shared_status, TaskStatus.TEXT_READY):
            episode = db.get_episode(video_id)
            captions_path_value = episode.get("captions_path") if episode else None
            captions_file = Path(captions_path_value) if captions_path_value else None
            whisper_lang = "zh" if platform != Platform.YOUTUBE else "en"
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
                    captions = await asyncio.to_thread(whisper_transcribe, audio_files[0], data_dir, language=whisper_lang)
                    raw_text = "\n".join(c.get("text", "") for c in captions)
                    captions_path = str(data_dir / "captions_en.json")
                    db.update_status(video_id, TaskStatus.TEXT_READY.value, captions_path=captions_path)
                    progress("转写完成")
                else:
                    # 尝试重新提取
                    progress("开始语音转写...")
                    progress("重新获取字幕...")
                    result = await asyncio.to_thread(youtube_extract, video_id, data_dir)
                    if result.subtitles:
                        raw_text = "\n".join(s.text for s in result.subtitles)
                        progress("转写完成")
                    elif result.audio_path:
                        progress("无字幕，开始语音转写...")
                        captions = await asyncio.to_thread(whisper_transcribe, result.audio_path, data_dir, language=whisper_lang)
                        raw_text = "\n".join(c.get("text", "") for c in captions)
                        captions_path = str(data_dir / "captions_en.json")
                        db.update_status(video_id, TaskStatus.TEXT_READY.value, captions_path=captions_path)
                        progress("转写完成")
                    else:
                        raise RuntimeError("无法获取字幕或音频")

            progress("清洗文本...")
            cleaned, speaker_count = clean_text(raw_text)
            clean_path = data_dir / "transcript_clean.txt"
            clean_path.write_text(cleaned, encoding="utf-8")
            db.update_status(
                video_id,
                TaskStatus.TEXT_READY.value,
                transcript_clean_path=str(clean_path),
            )
            shared_status = TaskStatus.TEXT_READY
            progress(f"文本清洗完成 ({len(cleaned)} 字符)")

        # --- Cancel check before translation ---
        if _check_cancel(cancel_event, progress):
            _discard_variant_workspace(target_dir, final_target_dir)
            if not atomic_regeneration:
                db.update_variant_status(
                    video_id, mode, TaskStatus.CANCELLED.value, error_message="用户取消"
                )
            return {"status": "cancelled", "video_id": video_id, "mode": mode, "output_path": None,
                    "message": "用户取消", "resumable_from": "text_ready"}

        # --- Stage: translate / rewrite ---
        if _should_run(variant_status, TaskStatus.TRANSLATED):
            episode = db.get_episode(video_id)
            clean_text_content = Path(episode["transcript_clean_path"]).read_text(encoding="utf-8")

            metadata = {
                "title": episode.get("title_original", ""),
                "channel": episode.get("channel_name", ""),
            }

            source_lang = "zh" if platform != Platform.YOUTUBE else "en"
            meta_file = data_dir / "metadata.json"
            if meta_file.exists():
                try:
                    loaded_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    if "source_language" in loaded_meta:
                        source_lang = loaded_meta["source_language"]
                except Exception:
                    pass

            translation_audit: dict = {}
            if source_lang == "zh" and mode == "faithful":
                progress("忠实模式：保留中文原意，规整文稿...")
                script_zh = clean_text_content
                # 中文源的忠实模式不经过翻译，没有可审计的译文
                translation_audit = {
                    "quality_status": "not_applicable",
                    "source_language": "zh",
                    "mode": "faithful",
                }
            else:
                action_name = "翻译" if source_lang == "en" else ("播客重述" if mode == "podcast" else "内容浓缩")
                progress(f"开始{action_name}（模式: {mode}）...")
                script_zh = await llm_translate_async(
                    clean_text_content,
                    mode,
                    metadata,
                    on_progress=progress,
                    llm_config=llm_config,
                    cancel_event=cancel_event,
                    on_audit=translation_audit.update,
                    source_language=source_lang,
                )
                if mode == "condensed":
                    async def rewrite(instruction: str) -> tuple[str, dict]:
                        retry_audit: dict = {}
                        text = await llm_translate_async(
                            clean_text_content,
                            mode,
                            metadata,
                            on_progress=progress,
                            llm_config=llm_config,
                            cancel_event=cancel_event,
                            on_audit=retry_audit.update,
                            source_language=source_lang,
                            extra_instruction=instruction,
                        )
                        return text, retry_audit

                    script_zh, chosen_audit = await _rewrite_if_condensed_too_long(
                        script_zh, clean_text_content, source_lang, data_dir, progress, rewrite,
                    )
                    if chosen_audit is not None:
                        translation_audit = chosen_audit
            _validate_translation_output(clean_text_content, script_zh, mode)
            _ensure_distinct_mode_script(video_id, mode, script_zh)
            script_path = target_dir / "script_zh.txt"
            script_path.write_text(script_zh, encoding="utf-8")
            audit_path = target_dir / "translation_audit.json"
            audit_path.write_text(
                json.dumps(translation_audit, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 缺少 quality_status 时不能默认「通过」——未执行的检查不给出通过声明
            audit_status = translation_audit.get(
                "quality_status", "unknown" if mode == "faithful" else "not_applicable"
            )
            audit_message = translation_audit.get("quality_message", "")
            _cleanup_variant_outputs(target_dir, resume_from="translated")

            # 标记翻译完成，管道即刻进入下一阶段
            if not atomic_regeneration:
                db.update_variant_status(
                    video_id,
                    mode,
                    TaskStatus.TRANSLATED.value,
                    script_zh_path=str(script_path),
                    audit_status=audit_status,
                    audit_message=audit_message,
                    translation_audit_path=str(audit_path),
                )
            variant_status = TaskStatus.TRANSLATED
            progress("翻译完成")

            # 后台异步：术语提取、质量自检、摘要生成（不阻塞管道）
            def _post_translate_async(summary_script_path: Path, summary_dir: Path):
                try:
                    _extract_terms(str(summary_script_path))
                except Exception as e:
                    logger.warning("术语提取失败（非致命）: %s", e)

                try:
                    transcript_clean_path = episode.get("transcript_clean_path", "")
                    if transcript_clean_path:
                        source_text = Path(transcript_clean_path).read_text(encoding="utf-8")
                        verdict = _quality_check(source_text, script_zh, metadata, llm_config=llm_config)
                        if verdict:
                            logger.info("质量检查结果: %d/%d 项通过", verdict["passed"], verdict["total"])
                except Exception as e:
                    logger.warning("质量检查失败（非致命）: %s", e)

                try:
                    summary = llm_summarize(script_zh, metadata, llm_config=llm_config)
                except Exception:
                    summary = {"title_zh": metadata.get("title", ""), "summary": "", "key_points": []}
                    logger.warning("摘要生成失败，使用空摘要")

                summary_path = summary_dir / "summary.json"
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                db.update_variant_artifacts(
                    video_id, mode,
                    summary_path=str(summary_path),
                )
                logger.info("后台摘要已写入: %s", summary_path)

            if atomic_regeneration:
                deferred_summary = (script_zh, metadata, _post_translate_async)
            else:
                threading.Thread(
                    target=_post_translate_async,
                    args=(script_path, target_dir),
                    daemon=True,
                ).start()

        # --- Cancel check before TTS ---
        if _check_cancel(cancel_event, progress):
            _cleanup_variant_outputs(target_dir, resume_from="translated")
            _discard_variant_workspace(target_dir, final_target_dir)
            if not atomic_regeneration:
                db.update_variant_status(
                    video_id, mode, TaskStatus.CANCELLED.value, error_message="用户取消"
                )
            return {"status": "cancelled", "video_id": video_id, "mode": mode, "output_path": None,
                    "message": "用户取消", "resumable_from": "translated"}

        # --- Stage: TTS ---
        if _should_run(variant_status, TaskStatus.TTS_DONE):
            script_path = target_dir / "script_zh.txt"
            script_zh = script_path.read_text(encoding="utf-8")

            episode = db.get_episode(video_id)
            source_path = Path(episode["transcript_clean_path"])
            source_text = source_path.read_text(encoding="utf-8")
            _validate_translation_output(source_text, script_zh, mode)
            _ensure_distinct_mode_script(video_id, mode, script_zh)
            progress("译文质量门禁通过")

            progress("TTS 文本清洗...")
            tts_text = clean_for_tts(script_zh)
            tts_text_path = target_dir / "tts_text.txt"
            tts_text_path.write_text(tts_text, encoding="utf-8")
            progress(_duration_estimate_message(mode, tts_text, data_dir))

            tts_dir = target_dir / "tts_segments"
            tts_dir.mkdir(exist_ok=True)

            progress("开始语音合成...")
            segments = await synthesize_async(tts_text, tts_dir, on_progress=progress, tts_config=tts_config)
            _check_synthesized_duration(tts_text, segments)

            # --- Cancel check before merge ---
            if _check_cancel(cancel_event, progress):
                _cleanup_variant_outputs(target_dir, resume_from="translated")
                _discard_variant_workspace(target_dir, final_target_dir)
                if not atomic_regeneration:
                    db.update_variant_status(
                        video_id, mode, TaskStatus.CANCELLED.value, error_message="用户取消"
                    )
                return {"status": "cancelled", "video_id": video_id, "mode": mode, "output_path": None,
                        "message": "用户取消", "resumable_from": "translated"}

            progress(f"合并 {len(segments)} 个音频片段...")
            episode = db.get_episode(video_id)
            # 优先用 DB 中的标题；为空时回退到 metadata.json → video_id
            raw_title = (episode.get("title_original") if episode else None) or ""
            if not raw_title.strip():
                meta_path = data_dir / "metadata.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        raw_title = meta.get("title", video_id)
                    except Exception:
                        raw_title = video_id
                else:
                    raw_title = video_id
            output_name = f"{_sanitize_filename(raw_title)}_{mode}"
            output_base = target_dir / output_name
            output_files = await merge_async(segments, output_base, on_progress=progress)

            if not atomic_regeneration:
                db.update_variant_status(
                    video_id,
                    mode,
                    TaskStatus.TTS_DONE.value,
                    tts_text_path=str(tts_text_path),
                    tts_segments_dir=str(tts_dir),
                    audio_zh_path=str(output_files[0]) if output_files else None,
                )
            variant_status = TaskStatus.TTS_DONE
            progress(f"MP3 已生成: {len(output_files)} 个文件")

        # --- Stage: done ---
        if atomic_regeneration:
            _commit_variant_workspace(target_dir, final_target_dir)
            committed_script = _path_in_committed_workspace(
                target_dir / "script_zh.txt", target_dir, final_target_dir,
            )
            committed_tts_text = _path_in_committed_workspace(
                target_dir / "tts_text.txt", target_dir, final_target_dir,
            )
            committed_tts_dir = _path_in_committed_workspace(
                target_dir / "tts_segments", target_dir, final_target_dir,
            )
            committed_audio = _path_in_committed_workspace(
                output_files[0] if output_files else None, target_dir, final_target_dir,
            )
            committed_summary = final_target_dir / "summary.json"
            committed_audit = final_target_dir / "translation_audit.json"
            db.update_variant_status(
                video_id,
                mode,
                TaskStatus.DONE.value,
                variant_dir=str(final_target_dir),
                script_zh_path=str(committed_script),
                summary_path=str(committed_summary) if committed_summary.is_file() else None,
                tts_text_path=str(committed_tts_text),
                tts_segments_dir=str(committed_tts_dir),
                audio_zh_path=str(committed_audio) if committed_audio else None,
                audit_status=audit_status,
                audit_message=audit_message,
                translation_audit_path=(
                    str(committed_audit) if committed_audit.is_file() else None
                ),
            )
            if deferred_summary:
                _, _, summary_worker = deferred_summary
                threading.Thread(
                    target=summary_worker,
                    args=(committed_script, final_target_dir),
                    daemon=True,
                ).start()
        else:
            db.update_variant_status(
                video_id, mode, TaskStatus.DONE.value,
                audit_status=audit_status,
                audit_message=audit_message,
                translation_audit_path=str(audit_path) if audit_path else None,
            )
        variant = db.get_variant(video_id, mode)
        output_path = variant.get("audio_zh_path", "") if variant else ""

        progress("处理完成！")
        return {
            "status": "done",
            "video_id": video_id,
            "mode": mode,
            "output_path": output_path,
            "message": "处理完成",
            "audit_status": audit_status,
            "audit_message": audit_message,
        }

    except Exception as e:
        error_msg = str(e)
        logger.exception("Pipeline failed for %s: %s", video_id, error_msg)
        _discard_variant_workspace(target_dir, final_target_dir)
        if shared_status != TaskStatus.TEXT_READY:
            db.update_status(video_id, TaskStatus.FAILED.value, error_message=error_msg)
        # 变体行必须记下自己的失败。历史列表读的是变体行状态，只写共享的 episode
        # 行会让失败的变体在历史里停在 new（绿点），而 /api/status 用 episode 行
        # 合成出的却是 failed —— 两个接口互相矛盾。
        #
        # 唯一例外：原子重跑且旧变体确实可用时，刻意保留旧成品，不用失败覆盖它
        # （见 test_failed_force_regeneration_preserves_previous_variant）。若旧变体
        # 本就没有可用产物，跳过写入会让用户重试失败后在历史里看不出任何变化。
        previous = db.get_variant(video_id, mode)
        previous_is_usable = bool(
            previous
            and previous.get("status") == TaskStatus.DONE.value
            and previous.get("audio_zh_path")
        )
        if not (atomic_regeneration and previous_is_usable):
            db.update_variant_status(
                video_id, mode, TaskStatus.FAILED.value, error_message=error_msg
            )
        progress(f"处理失败: {error_msg}")
        return {
            "status": "failed",
            "video_id": video_id,
            "mode": mode,
            "output_path": None,
            "message": error_msg,
        }


def _source_duration_seconds(data_dir: Path) -> float:
    try:
        meta = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
        return float(meta.get("duration_seconds") or 0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _duration_estimate_message(mode: str, tts_text: str, data_dir: Path) -> str:
    """合成前预估时长；超出该模式相对原视频的目标区间时提示（不阻塞）。"""
    estimate = predict_duration(tts_text)
    message = f"预计音频时长约 {estimate / 60:.1f} 分钟"
    source_seconds = _source_duration_seconds(data_dir)
    if source_seconds > 0:
        message += f"（原视频 {source_seconds / 60:.1f} 分钟）"
    budget = duration_budget(mode, source_seconds)
    if budget and not budget.within_target(estimate):
        message += (
            f"，偏离目标 {budget.target_min / 60:.0f}–{budget.target_max / 60:.0f} 分钟"
        )
        logger.warning("时长预算偏离: mode=%s %s", mode, message)
    return message


async def _rewrite_if_condensed_too_long(
    script: str,
    source_text: str,
    source_lang: str,
    data_dir: Path,
    progress: Callable[[str], None],
    rewrite: Callable[[str], Awaitable[tuple[str, dict]]],
) -> tuple[str, Optional[dict]]:
    """浓缩稿超出篇幅预算时带反馈重写一次；仍超标则取较短版本并提示，不让任务失败。

    返回 (文稿, 审计)。审计为 None 表示沿用首版审计；采用重写版时返回其审计，
    保证写盘的文稿与审计哈希一致。
    """
    source_seconds = _source_duration_seconds(data_dir)
    feedback = condensed_overshoot(source_text, script, source_lang, source_seconds)
    if not feedback:
        return script, None
    progress(f"浓缩篇幅超标：{feedback}，重写一次...")
    retried, retried_audit = await rewrite(
        f"【篇幅要求】{feedback}。请更大幅度地提炼：只保留最核心的观点、论证和关键案例，"
        "合并重复表达，把篇幅控制在目标范围内。"
    )
    remaining = condensed_overshoot(source_text, retried, source_lang, source_seconds)
    if remaining:
        progress(f"重写后仍超标：{remaining}，保留较短版本继续")
        logger.warning("浓缩重写后仍超标: %s", remaining)
    else:
        progress("重写后篇幅已达标")
    if len(retried) <= len(script):
        return retried, retried_audit
    return script, None


def _check_synthesized_duration(tts_text: str, segments: list[Path]) -> None:
    """合并前用预估时长校验合成结果，异常时让任务失败而不是产出坏音频。"""
    try:
        actual = sum(_probe_duration(Path(segment)) for segment in segments)
    except Exception:
        # 读不到时长（缺 ffprobe 等）时跳过，不因检查本身失败而误杀任务
        logger.warning("无法读取合成片段时长，跳过时长校验", exc_info=True)
        return
    error = synthesized_duration_error(predict_duration(tts_text), actual)
    if error:
        raise RuntimeError(error)


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
