/* ============================================================
   stream.js — SSE 实时进度 + 轮询降级 + 断线提示
   事件到 UI 的映射集中在 startProgressUI（main.js）注入的 handlers。
   ============================================================ */

import { streamURL } from './api.js';

let sseSource = null;
let pollTimer = null;
let elapsedTimer = null;
let pollFailures = 0;
let startedAt = 0;

/**
 * @param {object} ctx
 * @param ctx.vid string
 * @param ctx.mode string
 * @param ctx.timeline PipelineTimeline
 * @param ctx.onState ((state)=>void) 处理完整状态（UI 更新）
 * @param ctx.onDone ((data)=>void)   完成
 * @param ctx.onError ((data)=>void)  失败
 * @param ctx.onCancelled ((data)=>void) 取消
 * @param ctx.onNotFound (()=>void)   404 → 回表单
 * @param ctx.onInterrupted (()=>void) 首次断线提示
 */
export function startStream(ctx) {
  if (sseSource) { sseSource.close(); sseSource = null; }
  if ('EventSource' in window) {
    try {
      sseSource = new EventSource(streamURL(ctx.vid, ctx.mode));
      sseSource.addEventListener('state_snapshot', (e) => {
        const state = JSON.parse(e.data);
        if (state.stages && ctx.timeline) ctx.timeline.updateFromPoll(state.stages);
        ctx.onState(state);
      });
      sseSource.addEventListener('stage_change', (e) => {
        const d = JSON.parse(e.data);
        const t = ctx.timeline;
        if (d.status === 'active') t.setStageActive(d.stage_id);
        else if (d.status === 'done') t.setStageDone(d.stage_id, d.duration_s, d.meta);
        else if (d.status === 'reused') t.setStageReused(d.stage_id);
        else if (d.status === 'failed') t.setStageFailed(d.stage_id, d.message);
      });
      sseSource.addEventListener('stage_progress', (e) => {
        const d = JSON.parse(e.data);
        ctx.timeline.setStageProgress(d.stage_id, d.message, d.pct);
      });
      sseSource.addEventListener('translation_phase', (e) => {
        const d = JSON.parse(e.data);
        const suffix = d.total ? ` (${d.current || 0}/${d.total})` : '';
        ctx.timeline.setStageProgress(4, `${d.label || '翻译'}${suffix}`);
      });
      sseSource.addEventListener('log', (e) => {
        const d = JSON.parse(e.data);
        ctx.timeline.addLogEntry(d.stage_id, d.message);
      });
      sseSource.addEventListener('pipeline_complete', (e) => {
        const d = JSON.parse(e.data);
        stopAll();
        ctx.onDone(d);
      });
      sseSource.addEventListener('pipeline_error', (e) => {
        const d = JSON.parse(e.data);
        stopAll();
        ctx.timeline.setStageFailed(d.stage_id || ctx.timeline.currentStageId, d.message);
        ctx.onError(d);
      });
      sseSource.addEventListener('pipeline_cancelled', (e) => {
        const d = JSON.parse(e.data);
        stopAll();
        ctx.timeline.setStageCancelled(ctx.timeline.currentStageId || 1);
        ctx.onCancelled(d);
      });
      sseSource.onerror = () => {
        if (sseSource) { sseSource.close(); sseSource = null; }
        if (!pollTimer) startPolling(ctx);
      };
      return;
    } catch { /* 构造失败 → 降级轮询 */ }
  }
  startPolling(ctx);
}

export function startPolling(ctx) {
  if (pollTimer) return;
  pollFailures = 0;
  pollStatus(ctx);
  pollTimer = setInterval(() => pollStatus(ctx), 2000);
  if (startedAt) updateElapsed();
}

async function pollStatus(ctx) {
  try {
    const resp = await fetch(`/api/status/${ctx.vid}/${ctx.mode}`);
    if (resp.status === 404) { stopAll(); ctx.onNotFound(); return; }
    if (!resp.ok) { markInterrupted(ctx); return; }
    const hadFailures = pollFailures > 0;
    pollFailures = 0;
    const state = await resp.json();
    if (hadFailures && ctx.timeline) ctx.timeline.addLogEntry(ctx.timeline.currentStageId, '连接已恢复');
    if (state.stages && ctx.timeline) ctx.timeline.updateFromPoll(state.stages);
    ctx.onState(state);
  } catch {
    markInterrupted(ctx);
  }
}

function markInterrupted(ctx) {
  pollFailures++;
  ctx.onInterrupted && ctx.onInterrupted(pollFailures);
  if (ctx.timeline && pollFailures === 1) {
    ctx.timeline.addLogEntry(ctx.timeline.currentStageId, '连接暂时中断，正在自动重试…');
  }
}

export function startElapsed(ts) {
  startedAt = ts || Date.now() / 1000;
  updateElapsed();
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(updateElapsed, 5000);
}

function updateElapsed() {
  const el = document.getElementById('pElapsed');
  if (el) el.textContent = fmtClock((Date.now() / 1000) - startedAt);
}

export function setStartedAt(ts) { startedAt = ts; }

export function stopAll() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  pollFailures = 0;
}

export function isStreaming() { return !!(sseSource || pollTimer); }

function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
}