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

const MODE_ORDER = ['podcast', 'faithful', 'condensed'];

const PLATFORMS = {
  youtube: { label: 'YouTube', cls: 'thumb--youtube' },
  bilibili: { label: 'B站', cls: 'thumb--bilibili' },
  douyin: { label: '抖音', cls: 'thumb--douyin' },
  xiaohongshu: { label: '小红书', cls: 'thumb--xhs' },
};

const AUDIT_HTML = {
  passed: '<span class="tag tag--ok" title="全部片段均已翻译（确定性检查，非逐句语义审计）">完整性通过</span>',
  degraded: '<span class="tag tag--warn">建议抽查</span>',
};

function minutes(seconds) {
  if (!seconds) return '';
  return seconds < 60 ? '不到 1 分钟' : `${Math.round(seconds / 60)} 分钟`;
}

/** 报错只取首行并截短，完整内容放 title 悬停查看 */
function shortError(message) {
  const first = (message || '').split('\n')[0].trim();
  return first.length > 40 ? `${first.slice(0, 40)}…` : first;
}

function downloadLinks(v) {
  if (v.parts && v.parts.length > 1) {
    return v.parts.map((p, i) =>
      `<a class="btn btn--ghost btn--sm" href="${p.download_url}" title="${escHtml(p.filename)}">⬇ 第 ${i + 1} 集</a>`).join('');
  }
  const href = v.parts && v.parts.length ? v.parts[0].download_url : v.download_url;
  return `<a class="btn btn--ghost btn--sm" href="${href}">⬇ 下载</a>`;
}

/** 单个模式一行：状态决定展示内容与可用操作 */
function variantRow(v) {
  const meta = MODES[v.mode] || { label: v.mode, icon: '', badge: '' };
  const head = `<span class="badge ${meta.badge || ''}">${meta.icon} ${meta.label}</span>`;
  const del = `<button class="btn btn--ghost btn--sm btn--quiet-danger" data-del-variant="${v.mode}" title="删除该模式产物">删除</button>`;
  let body;
  let actions;
  if (v.status === 'done' && v.audio_zh_path) {
    const parts = v.parts && v.parts.length > 1 ? ` · ${v.parts.length} 集` : '';
    body = `<span class="variant-row__info">${minutes(v.output_seconds) || '已完成'}${parts}</span>${AUDIT_HTML[v.audit_status] || ''}`;
    actions = `<button class="btn btn--ghost btn--sm" data-play="${v.mode}">▶ 试听</button>${downloadLinks(v)}${del}`;
  } else if (v.status === 'processing' || v.status === 'queued') {
    body = `<span class="variant-row__info is-active">${v.status === 'queued' ? '排队中…' : '处理中…'}</span>`;
    actions = '';
  } else if (v.status === 'failed') {
    body = `<span class="variant-row__info is-failed" title="${escHtml(v.error_message)}">失败${v.error_message ? `：${escHtml(shortError(v.error_message))}` : ''}</span>`;
    actions = `<button class="btn btn--ghost btn--sm" data-gen="${v.mode}">↻ 重新生成</button>${del}`;
  } else {
    const label = v.status === 'cancelled' ? '已取消' : '未完成';
    body = `<span class="variant-row__info">${label}</span>`;
    actions = `<button class="btn btn--ghost btn--sm" data-gen="${v.mode}">继续生成</button>${del}`;
  }
  return `<div class="variant-row">${head}<span class="variant-row__body">${body}</span><span class="variant-row__actions">${actions}</span></div>`;
}

function thumb(ep) {
  const p = PLATFORMS[ep.platform] || PLATFORMS.youtube;
  const img = ep.thumbnail_url
    ? `<img src="${ep.thumbnail_url}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
    : '';
  return `<div class="history-card__thumb ${p.cls}"><span>${p.label}</span>${img}</div>`;
}

export function renderHistory() {
  const body = $('historyBody');
  body.innerHTML = '';
  if (!cache.length) {
    body.innerHTML = '<div class="history-empty">暂无历史记录<br>粘贴一条视频链接，生成你的第一期中文播客吧 🎧</div>';
    return;
  }
  cache.forEach((ep) => {
    const variants = MODE_ORDER
      .map((mode) => (ep.variants || []).find((v) => v.mode === mode))
      .filter(Boolean);
    const missing = MODE_ORDER.filter((mode) => !variants.some((v) => v.mode === mode));
    const platform = (PLATFORMS[ep.platform] || PLATFORMS.youtube).label;
    const titleHtml = ep.title_original
      ? escHtml(ep.title_original)
      : `<span class="muted">（标题未知）</span> <span class="history-card__id">${escHtml(ep.video_id)}</span>`;
    const metaItems = [
      platform,
      ep.channel_name ? escHtml(ep.channel_name) : '',
      ep.duration_seconds ? `原片 ${minutes(ep.duration_seconds)}` : '',
      (ep.updated_at || '').slice(0, 10),
    ].filter(Boolean);

    const card = document.createElement('div');
    card.className = 'card history-card';
    card.dataset.videoId = ep.video_id;
    card.innerHTML = `
      <div class="history-card__top">
        ${thumb(ep)}
        <div class="history-card__info">
          <div class="history-card__title" title="${escHtml(ep.title_original || ep.video_id)}">${titleHtml}</div>
          <div class="history-card__meta">${metaItems.map((m) => `<span>${m}</span>`).join('')}</div>
        </div>
        <button class="btn btn--ghost btn--sm btn--quiet-danger history-card__del" data-del-video="1" title="删除该视频及全部模式">删除整条</button>
      </div>
      ${variants.length ? `<div class="history-card__variants">${variants.map(variantRow).join('')}</div>` : ''}
      ${missing.length ? `<div class="history-card__more"><span>还可生成</span>${missing.map((mode) =>
        `<button class="badge badge--outline" data-gen="${mode}">＋ ${MODES[mode].label}</button>`).join('')}</div>` : ''}`;
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
        openPlayer({ vid, mode: variant.mode, title: card.querySelector('.history-card__title').getAttribute('title'), label: MODES[variant.mode].label, parts: variant.parts });
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