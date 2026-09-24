/* ============================================================
   history.js — 历史资产库抽屉
   以视频为卡片主体，展示多模式徽章与各自试听/下载/删除；
   未生成模式可点击预填回首屏快速生成（emit 'prefill'）。
   ============================================================ */

import { apiTasks, apiDeleteVariant, apiDeleteTask } from './api.js';
import { store } from './store.js';
import { openPlayer } from './player.js';
import { toast, confirmBox } from './modals.js';
import { MODES } from './config.js';
import { escHtml } from './timeline.js';

const $ = (id) => document.getElementById(id);

let cache = [];

const STATUS_BADGE = {
  done: 'badge--done',
  processing: 'badge--active',
  queued: 'badge--active',
  failed: 'badge--failed',
  cancelled: 'badge--cancelled',
};

/** 模式徽章：已生成 → 实心状态；未生成 → 虚线可点击 */
function modeBadge(v) {
  const meta = MODES[v.mode] || { label: v.mode, icon: '', badge: '' };
  if (v.status === 'done') {
    return `<span class="badge ${meta.badge || ''}">✓ ${meta.label}</span>`;
  }
  if (v.status) {
    return `<span class="badge ${STATUS_BADGE[v.status] || 'badge--pending'}">${meta.label} ${v.status === 'processing' ? '· 处理中' : v.status === 'queued' ? '· 排队' : ''}</span>`;
  }
  return `<button class="badge badge--outline" data-gen="${v.mode}">＋ ${meta.label}</button>`;
}

const AUDIT_HTML = {
  passed: '<span style="color:var(--color-success)">✅ 完整性通过</span>',
  degraded: '<span style="color:var(--color-warn)">⚠️ 建议抽查</span>',
};

function variantRow(v, ep) {
  const meta = MODES[v.mode] || { label: v.mode, icon: '' };
  const audit = v.audit_status ? AUDIT_HTML[v.audit_status] || '' : '';
  const isDone = v.status === 'done' && v.audio_zh_path;
  const download = v.parts && v.parts.length
    ? v.parts.map((p) => `<a class="btn btn--ghost btn--sm" href="${p.download_url}">⬇️ ${escHtml(p.filename)}</a>`).join('')
    : (isDone ? `<a class="btn btn--ghost btn--sm" href="${v.download_url}">⬇️ 下载 MP3</a>` : `<span style="color:var(--color-text-3);font-size:12px">未生成</span>`);
  return `
    <div class="variant-row">
      <span class="badge ${meta.badge || ''}">${meta.icon} ${meta.label}</span>
      ${audit ? `<span class="audit">${audit}</span>` : ''}
      <span class="spacer"></span>
      ${isDone ? `<button class="btn btn--ghost btn--sm" data-play="${v.mode}">▶️ 试听</button>` : ''}
      ${download}
      ${v.status && v.status !== 'processing' && v.status !== 'queued' ? `<button class="btn btn--ghost btn--sm" style="color:var(--color-danger)" data-del-variant="${v.mode}">删除</button>` : ''}
    </div>`;
}

export function renderHistory() {
  const body = $('historyBody');
  body.innerHTML = '';
  if (!cache.length) {
    body.innerHTML = '<div class="history-empty">暂无历史记录<br>粘贴一条 YouTube 链接，生成你的第一期中概播客吧 🎧</div>';
    return;
  }
  cache.forEach((ep) => {
    const variants = ep.variants || [];
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.videoId = ep.video_id;
    const estMB = ep.duration_seconds ? Math.round((ep.duration_seconds * 128) / 8 / 1024) : null;
    card.innerHTML = `
      <div class="history-card__top">
        <div class="history-card__thumb t${(variants.length % 3) + 1}">🎬</div>
        <div class="history-card__info">
          <div class="history-card__title" title="${escHtml(ep.title_original || ep.video_id)}">${escHtml(ep.title_original || ep.video_id)}</div>
          <div class="history-card__meta">
            <span>📺 ${escHtml(ep.channel_name || '未知频道')}</span>
            <span>⏱ ${ep.duration_seconds ? Math.floor(ep.duration_seconds / 60) + ' 分钟' : '未知'}</span>
            ${estMB ? `<span>📦 约 ${estMB} MB</span>` : ''}
            <span>📅 ${(ep.updated_at || '').slice(0, 10)}</span>
          </div>
        </div>
        <button class="btn btn--ghost btn--sm" style="color:var(--color-danger);flex:none" data-del-video="1">删除整条</button>
      </div>
      <div class="history-card__modes">
        ${['podcast', 'faithful', 'condensed'].map((mode) => {
          const v = variants.find((x) => x.mode === mode);
          return v ? modeBadge(v) : `<button class="badge badge--outline" data-gen="${mode}">＋ ${MODES[mode].label}</button>`;
        }).join('')}
      </div>
      ${variants.length ? `<div class="history-card__variants">${variants.map((v) => variantRow(v, ep)).join('')}</div>` : ''}`;
    body.appendChild(card);
  });
}

export async function openHistory() {
  document.getElementById('historyDrawer').classList.add('is-open');
  document.getElementById('historyOverlay').classList.add('is-open');
  $('historyBody').innerHTML = '<div class="history-empty">加载中…</div>';
  try {
    const { ok, data } = await apiTasks();
    if (!ok) throw new Error('failed');
    cache = data || [];
    renderHistory();
  } catch {
    $('historyBody').innerHTML = '<div class="history-empty">加载失败，请重试</div>';
  }
}

export function closeHistory() {
  document.getElementById('historyDrawer').classList.remove('is-open');
  document.getElementById('historyOverlay').classList.remove('is-open');
}

function refreshAfterMutation() {
  openHistory();
}

/** 初始化历史抽屉事件（main.js 调用一次） */
export function initHistory() {
  $('historyBtn').addEventListener('click', openHistory);
  $('historyBody').addEventListener('click', async (e) => {
    const t = e.target.closest('button, a');
    if (!t) return;

    // 预填回首屏生成新模式
    if (t.hasAttribute('data-gen')) {
      const card = t.closest('.card');
      const vid = card.dataset.videoId;
      closeHistory();
      store.emit('prefill', { vid, mode: t.dataset.gen });
      return;
    }

    // 试听
    if (t.hasAttribute('data-play')) {
      const card = t.closest('.card');
      const vid = card.dataset.videoId;
      const v = (cache.find((x) => x.video_id === vid) || {}).variants || [];
      const variant = v.find((x) => x.mode === t.dataset.play);
      if (variant && variant.audio_zh_path) {
        openPlayer({ vid, mode: variant.mode, title: card.querySelector('.history-card__title').textContent, label: MODES[variant.mode].label, parts: variant.parts });
      }
      return;
    }

    // 删除单个模式
    if (t.hasAttribute('data-del-variant')) {
      const card = t.closest('.card');
      const vid = card.dataset.videoId;
      const mode = t.dataset.delVariant;
      const ok = await confirmBox({
        icon: '🗑️',
        title: `删除「${MODES[mode].label}」？`,
        desc: '仅删除该模式的 MP3 与译文，视频的下载与转写共享素材将保留，日后可随时快速重新生成。',
        okText: '删除此模式',
      });
      if (!ok) return;
      try {
        const { ok: rOk, data } = await apiDeleteVariant(vid, mode);
        if (!rOk) { toast((data && data.error) || '删除失败', 'error'); return; }
        toast('已删除该模式产物，共享素材已保留', 'success');
      } catch {
        toast('网络错误，删除失败', 'error');
      }
      refreshAfterMutation();
      return;
    }

    // 删除整条
    if (t.hasAttribute('data-del-video')) {
      const card = t.closest('.card');
      const vid = card.dataset.videoId;
      const ok = await confirmBox({
        icon: '⚠️',
        title: '彻底删除该视频？',
        desc: '将删除全部模式产物、原始音频大文件与数据库记录，此操作不可恢复。',
        okText: '彻底删除',
      });
      if (!ok) return;
      try {
        const { ok: rOk, data } = await apiDeleteTask(vid);
        if (!rOk) { toast((data && data.error) || '删除失败', 'error'); return; }
        toast('已彻底删除该视频及全部素材', 'success');
      } catch {
        toast('网络错误，删除失败', 'error');
      }
      refreshAfterMutation();
    }
  });
}