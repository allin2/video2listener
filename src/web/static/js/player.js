/* ============================================================
   player.js — 吸底播放器单例
   src 直接指向 /api/download/{vid}/{mode}（可分卷 ?part=），
   支持 播放/暂停/拖动/倍速/分卷切换。
   ============================================================ */

import { downloadURL } from './api.js';
import { fmtClock } from './timeline.js';

const RATES = [1, 1.25, 1.5, 2];

let audio = null;
let current = null; // { vid, mode, parts: [{filename}], partIndex }
let rate = 1;

const $ = (sel) => document.querySelector(sel.startsWith('#') ? sel : `#${sel}`);

function ensureAudio() {
  if (audio) return audio;
  audio = new Audio();
  audio.preload = 'metadata';
  audio.addEventListener('timeupdate', renderTime);
  audio.addEventListener('loadedmetadata', () => {
    $('plDur').textContent = fmtClock(audio.duration);
    $('plSeek').max = audio.duration || 100;
  });
  audio.addEventListener('ended', () => {
    $('plPlay').textContent = '▶';
  });
  $('plSeek').addEventListener('input', () => {
    if (audio) audio.currentTime = parseFloat($('plSeek').value);
  });
  return audio;
}

function renderTime() {
  if (!audio) return;
  $('plCur').textContent = fmtClock(audio.currentTime);
  if (audio.duration) $('plSeek').value = audio.currentTime;
}

function renderSub() {
  if (!current) return;
  const base = current.baseLabel || '';
  const partName = current.parts.length > 1 ? ` · Part ${current.partIndex + 1}/${current.parts.length}` : '';
  $('#plSub').textContent = base + partName;
}

/**
 * 打开播放器并开始播放
 * @param {{vid:string, mode:string, title:string, label:string, parts?:{filename:string}[]}} cfg
 */
export function openPlayer(cfg) {
  const a = ensureAudio();
  current = {
    vid: cfg.vid,
    mode: cfg.mode,
    parts: (cfg.parts && cfg.parts.length) ? cfg.parts : [{ filename: null }],
    partIndex: 0,
  };
  $('plTitle').textContent = cfg.title || '';
  current.baseLabel = (cfg.label || '') + ' · 中文';
  renderSub();

  // 分卷选择器
  const partSel = $('plPart');
  if (current.parts.length > 1) {
    partSel.hidden = false;
    partSel.innerHTML = current.parts.map((p, i) =>
      `<option value="${i}">Part ${i + 1}</option>`).join('');
    partSel.onchange = () => {
      current.partIndex = parseInt(partSel.value, 10);
      loadPart();
    };
  } else {
    partSel.hidden = true;
    partSel.onchange = null;
  }

  loadPart();
  document.getElementById('playerBar').classList.add('is-open');
}

function loadPart() {
  if (!current) return;
  const a = ensureAudio();
  const part = current.parts[current.partIndex] || {};
  a.src = downloadURL(current.vid, current.mode, part.filename);
  a.playbackRate = rate;
  $('#plRate').textContent = rate + 'x';
  renderSub();
  a.play().catch(() => { /* 用户手势后可再播放 */ });
  $('plPlay').textContent = '⏸';
}

export function closePlayer() {
  if (audio) {
    audio.pause();
    audio.src = '';
  }
  current = null;
  document.getElementById('playerBar').classList.remove('is-open');
  $('plPlay').textContent = '▶';
}

export function isPlayerOpen() {
  return document.getElementById('playerBar').classList.contains('is-open');
}

/** 初始化播放器事件（main.js 调用一次） */
export function initPlayer() {
  $('plPlay').addEventListener('click', () => {
    if (!audio) return;
    if (audio.paused) {
      audio.play().then(() => { $('plPlay').textContent = '⏸'; }).catch(() => {});
    } else {
      audio.pause();
      $('plPlay').textContent = '▶';
    }
  });
  $('plRate').addEventListener('click', () => {
    rate = RATES[(RATES.indexOf(rate) + 1) % RATES.length];
    $('#plRate').textContent = rate + 'x';
    if (audio) audio.playbackRate = rate;
  });
  $('plClose').addEventListener('click', closePlayer);
  ensureAudio();
}