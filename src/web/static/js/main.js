/* ============================================================
   main.js — 入口装配：主题 / 模式选择 / 提交链路（预检三分支）
   / 进度视图 / 完成视图 / 失败视图 / 会话恢复 / 取消与断点重试
   ============================================================ */

import { store } from './store.js';
import { apiProcess, apiStatus, apiTasks, apiVariantText, downloadURL } from './api.js';
import { MODES, SSE_STAGES, collectConfig, saveConfig } from './config.js';
import { PipelineTimeline, escHtml } from './timeline.js';
import { startStream, startElapsed, stopAll } from './stream.js';
import { initSettings } from './settings.js';
import { initHistory, closeHistory } from './history.js';
import { initPlayer, openPlayer } from './player.js';
import { toast, showDecision, closeDecision, confirmBox, bindModalEsc } from './modals.js';
import {
  titleIdle, titleProgress, titleDone, titleFailed,
  requestNotificationPermission, fireNotification,
} from './notify.js';

const $ = (id) => document.getElementById(id);

/* ── 运行时状态（任务维度） ─────────────────────────────── */
let timeline = null;
let currentTitle = '';
let summaryPollTimer = null;

const SESSION_KEYS = { vid: 'v2l_videoId', mode: 'v2l_mode', startedAt: 'v2l_startedAt', resume: 'v2l_resumeFrom' };

/* ═══════════════ 视图切换 ═══════════════ */
const VIEW_ID = { submit: 'viewSubmit', progress: 'viewProgress', result: 'viewResult', error: 'viewError' };
function switchViewById(view) {
  Object.entries(VIEW_ID).forEach(([k, id]) => { $(id).hidden = k !== view; });
  store.setState({ view });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setStatusBadge(text, cls) {
  const el = $('pStatus');
  el.textContent = text;
  el.className = 'badge ' + cls;
}

/* ═══════════════ 主题 ═══════════════ */
function initTheme() {
  const saved = localStorage.getItem('v2l_theme');
  const theme = saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = theme;
  $('themeBtn').textContent = theme === 'dark' ? '☀️' : '🌙';
  $('themeBtn').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('v2l_theme', next);
    $('themeBtn').textContent = next === 'dark' ? '☀️' : '🌙';
  });
}

/* ═══════════════ 模式选择卡片 ═══════════════ */
function selectModeCard(mode) {
  document.querySelectorAll('.mode-card').forEach((c) => {
    const on = c.dataset.mode === mode;
    c.classList.toggle('is-selected', on);
    c.setAttribute('aria-checked', String(on));
  });
}

function initModeCards() {
  document.querySelectorAll('.mode-card').forEach((card) => {
    card.addEventListener('click', () => selectModeCard(card.dataset.mode));
    card.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const cards = [...document.querySelectorAll('.mode-card')];
        const i = cards.indexOf(card);
        const next = cards[(i + (e.key === 'ArrowRight' ? 1 : -1) + cards.length) % cards.length];
        next.focus();
        next.click();
      }
    });
  });
}

/* ═══════════════ URL 输入 ═══════════════ */
function initUrlInput() {
  const input = $('urlInput');
  const clear = $('urlClearBtn');
  const YT_RE = /^(https?:\/\/)?(www\.)?(youtube\.com\/watch\?v=|youtu\.be\/)[\w-]{11}/i;
  const ID_RE = /^[\w-]{11}$/;
  const BILI_RE = /(bilibili\.com\/video\/|b23\.tv\/|BV[a-zA-Z0-9]{10})/i;
  const DOUYIN_RE = /(douyin\.com\/video\/|v\.douyin\.com\/)/i;
  const XHS_RE = /(xiaohongshu\.com\/|xhslink\.com\/)/i;
  const URL_HTTP_RE = /https?:\/\/[^\s<>"']+/i;

  input.addEventListener('input', () => {
    clear.hidden = !input.value;
    $('urlWrap').classList.remove('error');
    $('urlErr').hidden = true;
  });
  clear.addEventListener('click', () => { input.value = ''; clear.hidden = true; input.focus(); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitBtn.click(); });
  window.validateUrl = (v) => {
    const s = (v || '').trim();
    return YT_RE.test(s) || ID_RE.test(s) || BILI_RE.test(s) || DOUYIN_RE.test(s) || XHS_RE.test(s) || URL_HTTP_RE.test(s);
  };
}

/* ═══════════════ 提交链路 ═══════════════ */
function buildPayload(action) {
  const cfg = collectConfig();
  return {
    url: $('urlInput').value.trim(),
    mode: document.querySelector('.mode-card.is-selected').dataset.mode,
    action: action || '',
    resume_from: sessionStorage.getItem(SESSION_KEYS.resume) || '',
    api_key: cfg.llmApiKey,
    base_url: cfg.llmBaseUrl,
    model: cfg.model,
    tts_api_key: cfg.ttsApiKey,
    tts_base_url: cfg.ttsBaseUrl,
    tts_model: cfg.ttsModel,
    tts_voice: cfg.ttsVoice,
    tts_speed: cfg.ttsSpeed != null ? cfg.ttsSpeed : 1.0,
  };
}

/** 输入校验；LLM 配置缺失时自动滑出设置抽屉 */
function validateBeforeSubmit() {
  if (!window.validateUrl($('urlInput').value)) {
    $('urlWrap').classList.add('error');
    $('urlErr').hidden = false;
    $('urlInput').focus();
    return false;
  }
  const cfg = collectConfig();
  const missings = [];
  if (!cfg.llmApiKey) missings.push('llmKey');
  if (!cfg.model) missings.push('llmModel');
  if (missings.length) {
    toast('请先在设置中配置 LLM API Key 与模型', 'warn');
    openDrawer('settings');
    // 高亮缺失字段
    missings.forEach((id) => {
      const field = $(id).closest('.field');
      if (field) { field.classList.remove('field--error'); field.classList.add('field--error'); }
    });
    return false;
  }
  return true;
}

async function submitTask(action) {
  const btn = $('submitBtn');
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const payload = buildPayload(action);
    const { ok, status, data } = await apiProcess(payload);
    if (status === 409 && data && (data.status === 'queued' || data.status === 'processing')) {
      // 分支：任务进行中 → 恢复监控
      btn.disabled = false;
      btn.textContent = '开始处理 →';
      restoreTaskSession(data.video_id, data.mode);
      timeline && timeline.addLogEntry(timeline.currentStageId || 1, 'ℹ️ 该任务正在处理中，已为你恢复实时监控');
      return;
    }
    if (data && (data.status === 'variant_exists' || data.status === 'reuse_available')) {
      btn.disabled = false;
      btn.textContent = '开始处理 →';
      showDecisionModal(data);
      return;
    }
    if (!ok) {
      btn.disabled = false;
      btn.textContent = '开始处理 →';
      showErrorMsg(data && (data.error || data.message) ? (data.error || data.message) : '提交失败');
      return;
    }
    requestNotificationPermission();
    startProgressUI(data);
  } catch {
    btn.disabled = false;
    btn.textContent = '开始处理 →';
    showErrorMsg('网络异常，请确认服务运行正常后重试');
  }
}

/** 智能拦截决策弹窗 */
function showDecisionModal(data) {
  const mode = MODES[data.mode] || { label: data.mode, full: data.mode };
  if (data.status === 'reuse_available') {
    showDecision({
      icon: '⚡',
      title: '共享素材已就绪',
      desc: `该视频已完成下载与转写，前三步 100% 缓存复用，可直接快进到翻译阶段，大幅节省时间。${data.message || ''}`,
      actions: [
        { label: '⚡ 立即快速生成', cls: 'btn--primary', fn: () => submitTask('create_variant') },
        { label: '取消', cls: 'btn--ghost', fn: () => {} },
      ],
    });
  } else {
    const actions = [];
    if (data.output_path) {
      actions.push({ label: '⬇️ 下载已有文件', cls: 'btn--primary', fn: () => { window.location.href = downloadURL(data.video_id, data.mode); } });
    }
    actions.push({ label: '🔄 重新生成', cls: 'btn--secondary', fn: () => submitTask('regenerate_variant') });
    actions.push({ label: '取消', cls: 'btn--ghost', fn: () => {} });
    showDecision({
      icon: '✅',
      title: `该视频的「${mode.full}」已生成`,
      desc: (data.message || '') + (data.output_path ? ' 可直接下载已有文件，或基于最新配置重新生成。' : ''),
      actions,
    });
  }
}

/* ═══════════════ 进度视图 ═══════════════ */
function startProgressUI(data) {
  const vid = data.video_id;
  const mode = data.mode;
  store.setState({ task: { videoId: vid, mode, startedAt: Date.now() / 1000 }, phase: 'processing' });
  sessionStorage.setItem(SESSION_KEYS.vid, vid);
  sessionStorage.setItem(SESSION_KEYS.mode, mode);
  sessionStorage.setItem(SESSION_KEYS.startedAt, String(Date.now() / 1000));
  sessionStorage.removeItem(SESSION_KEYS.resume);

  currentTitle = '';
  switchViewById('progress');
  $('pModeBadge').className = `badge ${MODES[mode].badge}`;
  $('pModeBadge').textContent = `${MODES[mode].icon} ${MODES[mode].label}`;
  $('pTitle').textContent = `正在处理 ${vid}`;
  setStatusBadge('排队中…', 'badge--active');
  $('pElapsed').textContent = '00:00';
  $('connTag').hidden = true;

  timeline = new PipelineTimeline($('timeline'), {
    onCancel: () => requestCancel(),
    onRetry: (stageId) => retryFromStage(stageId),
  });
  titleProgress();

  startElapsed(Date.now() / 1000);
  startStream({
    vid,
    mode,
    get timeline() { return timeline; },
    onState: (state) => updateUI(state),
    onDone: (d) => onPipelineDone(d),
    onError: () => {}, // SSE 内已标记失败 stage，UI 由 onState(failed) 兜底
    onCancelled: (d) => {
      setStatusBadge('已取消', 'badge--failed');
      $('connTag').hidden = true;
      titleFailed();
      cleanupSession();
    },
    onNotFound: () => { toast('任务不存在或已被删除', 'warn'); returnToForm(false); },
    onInterrupted: (n) => {
      $('connTag').hidden = false;
      setStatusBadge('重新连接中…', 'badge--active');
      if (n === 1) toast('连接暂时中断，正在自动重试', 'warn');
    },
  });
}

/** 处理状态更新（轮询 / state_snapshot 共用） */
function updateUI(state) {
  const statusMap = {
    queued: ['排队中…', 'badge--active'],
    processing: ['处理中…', 'badge--active'],
    done: ['已完成', 'badge--done'],
    failed: ['失败', 'badge--failed'],
    cancelled: ['已取消', 'badge--failed'],
  };
  const s = statusMap[state.status];
  if (s && ($('pStatus').textContent !== s[0])) setStatusBadge(s[0], s[1]);
  if (state.status === 'done') {
    stopAll();
    renderResult({ vid: state.video_id, mode: state.mode, audit_status: state.audit_status, audit_message: state.audit_message });
    if (!state.summary_path) startSummaryPolling(state.video_id, state.mode);
  }
  if (state.status === 'failed') {
    stopAll();
    showErrorMsg(state.error_message || '处理失败');
  }
}

function onPipelineDone(d) {
  const vid = d.video_id, mode = d.mode;
  currentTitle = d.title || vid;
  // 全部阶段标记完成（保留 reused）
  SSE_STAGES.forEach((s) => {
    if (timeline && timeline.stages.get(s.id)?.status !== 'reused') timeline?.setStageDone(s.id);
  });
  timeline && timeline.clearAllActions();
  stopAll();
  titleDone(currentTitle);
  fireNotification(currentTitle, vid);
  renderResult({ vid, mode, title: currentTitle, audit_status: d.audit_status, audit_message: d.audit_message });
  // 摘要后台生成：轮询 status 的 summary_path
  apiStatus(vid, mode).then(({ data }) => { if (data && !data.summary_path) startSummaryPolling(vid, mode); });
}

/* ═══════════════ 完成视图 ═══════════════ */
async function renderResult({ vid, mode, title, audit_status, audit_message }) {
  switchViewById('result');
  store.setState({ phase: 'done' });
  cleanupSession();

  // 从 /api/tasks 获取标题与分卷信息
  let epTitle = title || '';
  let parts = [];
  try {
    const { data } = await apiTasks();
    const ep = (data || []).find((x) => x.video_id === vid);
    if (ep) {
      epTitle = epTitle || ep.title_original || vid;
      const v = (ep.variants || []).find((x) => x.mode === mode);
      if (v) parts = v.parts || [];
    }
  } catch { /* 兜底 */ }
  if (!epTitle) epTitle = vid;
  currentTitle = epTitle;
  titleDone(epTitle);

  $('rTitle').textContent = epTitle;
  $('rMeta').innerHTML = [
    `<span>${MODES[mode].icon} ${MODES[mode].full}</span>`,
    `<span>🕐 视频 ${vid}</span>`,
  ].join('');

  // 审计横幅
  const ab = $('auditBanner');
  if (mode === 'faithful' && audit_status) {
    ab.hidden = false;
    if (audit_status === 'passed') {
      ab.className = 'audit-banner audit-banner--passed';
      ab.innerHTML = `<span style="font-size:18px">✅</span><span><b>完整性检查通过</b> — ${escHtml(audit_message || '全部片段均已翻译')}。仅做确定性检查，未逐句语义审计。</span>`;
    } else if (audit_status === 'degraded') {
      ab.className = 'audit-banner audit-banner--degraded';
      ab.innerHTML = `<span style="font-size:18px">⚠️</span><span><b>建议抽查</b> — ${escHtml(audit_message || '音频已生成可下载，但部分确定性检查未达阈值')}</span>`;
    } else {
      ab.hidden = true;
    }
  } else {
    ab.hidden = true;
  }

  // 下载列表（单文件或多 Part）
  const dl = $('dlList');
  dl.innerHTML = '';
  if (parts && parts.length) {
    parts.forEach((p) => {
      const d = document.createElement('div');
      d.className = 'dl-item';
      d.innerHTML = `<span>🎵</span><span class="name">${escHtml(p.filename)}</span><span class="spacer"></span><button class="btn btn--secondary btn--sm">⬇️ 下载</button>`;
      d.querySelector('button').onclick = () => { window.location.href = downloadURL(vid, mode, p.filename); };
      dl.appendChild(d);
    });
  } else {
    const d = document.createElement('div');
    d.className = 'dl-item';
    d.innerHTML = `<span>🎵</span><span class="name">${escHtml(epTitle)}.mp3</span><span class="spacer"></span><button class="btn btn--secondary btn--sm">⬇️ 下载</button>`;
    d.querySelector('button').onclick = () => { window.location.href = downloadURL(vid, mode); };
    dl.appendChild(d);
  }

  // 试听
  $('playBtn').onclick = () => {
    openPlayer({ vid, mode, title: epTitle, label: MODES[mode].label, parts });
  };

  // 复制脚本（脚本存在时显示）
  $('copyScriptBtn').hidden = false;
  $('copyScriptBtn').onclick = async () => {
    try {
      const resp = await apiVariantText(vid, mode, 'script');
      if (!resp.ok) { toast('脚本尚未生成', 'warn'); return; }
      const text = await resp.text();
      await navigator.clipboard.writeText(text);
      toast('中文脚本已复制到剪贴板', 'success');
    } catch { toast('复制失败', 'error'); }
  };

  // Tab：懒加载脚本/摘要
  bindResultTabs(vid, mode);
  loadResultTab(vid, mode, 'script');
}

let resultTabKind = 'script';
function bindResultTabs(vid, mode) {
  $('tabScriptBtn').onclick = () => { setActiveTab('script'); loadResultTab(vid, mode, 'script'); };
  $('tabSummaryBtn').onclick = () => { setActiveTab('summary'); loadResultTab(vid, mode, 'summary'); };
}
function setActiveTab(kind) {
  resultTabKind = kind;
  $('tabScriptBtn').classList.toggle('is-active', kind === 'script');
  $('tabSummaryBtn').classList.toggle('is-active', kind === 'summary');
}
async function loadResultTab(vid, mode, kind) {
  const panel = $('tabPanelEl');
  panel.hidden = false;
  panel.textContent = '加载中…';
  try {
    const resp = await apiVariantText(vid, mode, kind);
    if (!resp.ok) {
      panel.textContent = kind === 'summary' ? '摘要尚在生成中（后台异步生成），稍后刷新查看' : '中文脚本尚未生成';
      return;
    }
    panel.textContent = await resp.text();
  } catch {
    panel.textContent = kind === 'summary' ? '摘要加载失败' : '脚本加载失败';
  }
}

/** 摘要后台轮询：等待 status.summary_path 出现 */
function startSummaryPolling(vid, mode) {
  if (summaryPollTimer) return;
  const start = Date.now();
  summaryPollTimer = setInterval(async () => {
    try {
      const { ok, data } = await apiStatus(vid, mode);
      if (!ok) return;
      if (data.summary_path || data.status === 'failed' || data.status === 'cancelled') {
        clearInterval(summaryPollTimer);
        summaryPollTimer = null;
        if (resultTabKind === 'summary') loadResultTab(vid, mode, 'summary');
        return;
      }
      if (Date.now() - start > 120000) {
        clearInterval(summaryPollTimer);
        summaryPollTimer = null;
      }
    } catch { /* 忽略单次失败 */ }
  }, 3000);
}

/* ═══════════════ 失败视图 ═══════════════ */
function showErrorMsg(msg) {
  switchViewById('error');
  $('errMsg').textContent = msg;
  titleFailed();
  cleanupSession();
}

/* ═══════════════ 会话 / 返回表单 ═══════════════ */
function cleanupSession() {
  sessionStorage.removeItem(SESSION_KEYS.vid);
  sessionStorage.removeItem(SESSION_KEYS.mode);
  sessionStorage.removeItem(SESSION_KEYS.startedAt);
}

function returnToForm(clearUrl) {
  stopAll();
  if (timeline) { timeline.destroy(); timeline = null; }
  switchViewById('submit');
  $('submitBtn').disabled = false;
  $('submitBtn').textContent = '开始处理 →';
  $('errMsg').textContent = '';
  if (clearUrl) { $('urlInput').value = ''; $('urlClearBtn').hidden = true; }
  cleanupSession();
  titleIdle();
  $('urlInput').focus();
}

async function restoreTaskSession(savedVid, savedMode) {
  const mode = savedMode || 'podcast';
  selectModeCard(mode);
  store.setState({ task: { videoId: savedVid, mode, startedAt: Date.now() / 1000 }, phase: 'processing' });
  switchViewById('progress');
  $('pModeBadge').className = `badge ${MODES[mode].badge}`;
  $('pModeBadge').textContent = `${MODES[mode].icon} ${MODES[mode].label}`;
  $('pTitle').textContent = `正在处理 ${savedVid}`;
  setStatusBadge('恢复中…', 'badge--active');
  $('connTag').hidden = true;
  timeline = new PipelineTimeline($('timeline'), {
    onCancel: () => requestCancel(),
    onRetry: (stageId) => retryFromStage(stageId),
  });
  try {
    const { ok, status, data } = await apiStatus(savedVid, mode);
    if (!ok) { returnToForm(false); return; }
    if (data.stages) timeline.updateFromPoll(data.stages);
    const ts = parseFloat(sessionStorage.getItem('v2l_startedAt') || '0') || data.started_at || Date.now() / 1000;
    startElapsed(ts);
    startStream({
      vid: savedVid, mode,
      get timeline() { return timeline; },
      onState: (s) => updateUI(s),
      onDone: (d) => onPipelineDone(d),
      onError: () => {},
      onCancelled: (d) => { setStatusBadge('已取消', 'badge--failed'); cleanupSession(); titleFailed(); },
      onNotFound: () => returnToForm(false),
      onInterrupted: () => { $('connTag').hidden = false; },
    });
  } catch {
    returnToForm(false);
  }
}

/* ═══════════════ 取消 / 断点重试 ═══════════════ */
async function requestCancel() {
  const task = store.getState().task;
  if (!task) return;
  const ok = await confirmBox({
    icon: '⏹',
    title: '取消当前任务？',
    desc: '将安全中止处理进程，已下载的素材会保留，之后可从断点继续。',
    okText: '确认取消任务',
  });
  if (!ok) return;
  try {
    await fetch(`/api/cancel/${task.videoId}/${task.mode}`, { method: 'POST' });
    toast('已请求取消，正在安全中止…', 'info');
  } catch {
    toast('取消请求失败', 'error');
  }
}

function retryFromStage(stageId) {
  // stage_id → resume_from 字符串
  const resumeMap = { 3: 'metadata_fetched', 4: 'text_ready', 5: 'translated' };
  sessionStorage.setItem(SESSION_KEYS.resume, resumeMap[stageId] || '');
  returnToForm(false);
  setTimeout(() => submitTask('regenerate_variant'), 120);
}

/* ═══════════════ 抽屉开关 ═══════════════ */
function openDrawer(name) {
  $(`${name}Drawer`).classList.add('is-open');
  $(`${name}Overlay`).classList.add('is-open');
}
function closeDrawer(name) {
  $(`${name}Drawer`).classList.remove('is-open');
  $(`${name}Overlay`).classList.remove('is-open');
}

function initLayers() {
  $('settingsBtn').addEventListener('click', () => openDrawer('settings'));
  $('hintSettingsBtn').addEventListener('click', () => openDrawer('settings'));
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-close]');
    if (!c) return;
    const name = c.dataset.close;
    if (name === 'settings') closeDrawer('settings');
    else if (name === 'history') closeHistory();
    else if (name === 'decision') closeDecision();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeDrawer('settings');
    closeHistory();
    if (!document.getElementById('confirmModal').hidden) return; // confirm 由 modals 管理
  });
}

/* ═══════════════ 启动 ═══════════════ */
function init() {
  initTheme();
  initModeCards();
  initUrlInput();
  initSettings();
  initHistory();
  initPlayer();
  initLayers();
  bindModalEsc();

  $('submitBtn').addEventListener('click', () => {
    if (!validateBeforeSubmit()) return;
    saveConfig();
    submitTask('');
  });
  $('newTaskBtn').addEventListener('click', () => returnToForm(true));
  $('errBackBtn').addEventListener('click', () => returnToForm(false));
  $('errRetryBtn').addEventListener('click', () => returnToForm(false));

  // 历史抽屉“＋ 未生成模式” → 预填回首屏并提交预检
  store.on('prefill', ({ vid, mode }) => {
    if (vid.startsWith('BV')) {
      $('urlInput').value = `https://www.bilibili.com/video/${vid}`;
    } else if (vid.startsWith('dy_')) {
      $('urlInput').value = `https://www.douyin.com/video/${vid.slice(3)}`;
    } else if (vid.startsWith('xhs_')) {
      $('urlInput').value = `https://www.xiaohongshu.com/discovery/item/${vid.slice(4)}`;
    } else {
      $('urlInput').value = `https://www.youtube.com/watch?v=${vid}`;
    }
    selectModeCard(mode);
    switchViewById('submit');
    $('urlClearBtn').hidden = false;
    saveConfig();
    submitTask('');
  });

  // 刷新后恢复进行中任务
  const savedVid = sessionStorage.getItem(SESSION_KEYS.vid);
  const savedMode = sessionStorage.getItem(SESSION_KEYS.mode);
  if (savedVid) restoreTaskSession(savedVid, savedMode);
}

init();