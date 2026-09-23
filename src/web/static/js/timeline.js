/* ============================================================
   timeline.js — 6 阶段流水线时间线（迁移自旧 PipelineTimeline，
   适配新 DOM 结构：行内进度条 / 复用缓存徽章 / 日志展开 / 动作按钮）
   ============================================================ */

import { SSE_STAGES } from './config.js';

export class PipelineTimeline {
  /**
   * @param {HTMLElement} container
   * @param {{ onCancel?: (stageId:number)=>void, onRetry?: (stageId:number)=>void }} hooks
   */
  constructor(container, hooks = {}) {
    this.container = container;
    this.hooks = hooks;
    this.stages = new Map(); // id -> {el, dotEl, nameEl, timeEl, msgEl, barEl, barFillEl, actionsEl, logWrapEl, logEl, status}
    this.currentStageId = 0;
    this._build();
  }

  _build() {
    this.container.innerHTML = '';
    SSE_STAGES.forEach((s) => {
      const li = document.createElement('li');
      li.className = 'stage stage--pending';

      const dot = document.createElement('div');
      dot.className = 'stage__dot';
      dot.textContent = s.icon;

      const head = document.createElement('div');
      head.className = 'stage__head';
      const name = document.createElement('span');
      name.className = 'stage__name';
      name.textContent = s.name;
      const time = document.createElement('span');
      time.className = 'stage__time';
      const toggle = document.createElement('button');
      toggle.className = 'stage__toggle';
      toggle.textContent = '日志 ▾';
      head.append(name, time, toggle);

      const msg = document.createElement('div');
      msg.className = 'stage__msg';

      const bar = document.createElement('div');
      bar.className = 'stage__bar';
      bar.hidden = true;
      const fill = document.createElement('i');
      fill.style.width = '0%';
      bar.appendChild(fill);

      const actions = document.createElement('div');
      actions.className = 'stage__actions';

      const logsWrap = document.createElement('div');
      logsWrap.className = 'stage__logs';
      const logs = document.createElement('div');
      logs.className = 'stage__logs-inner';
      logsWrap.appendChild(logs);

      li.append(dot, head, msg, bar, actions, logsWrap);
      this.container.appendChild(li);

      this.stages.set(s.id, {
        el: li, dotEl: dot, nameEl: name, timeEl: time, msgEl: msg,
        barEl: bar, barFillEl: fill, actionsEl: actions, logWrapEl: logsWrap, logEl: logs,
        status: 'pending',
      });

      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        this._toggleLog(s.id);
      });
    });
  }

  _toggleLog(id) {
    const st = this.stages.get(id);
    if (!st || st.status === 'pending') return;
    this.stages.forEach((v, k) => { if (k !== id) v.logWrapEl.classList.remove('open'); });
    st.logWrapEl.classList.toggle('open');
    st.logEl.querySelectorAll('.stage__toggle');
    const t = st.el.querySelector('.stage__toggle');
    t.textContent = st.logWrapEl.classList.contains('open') ? '日志 ▴' : '日志 ▾';
    if (st.logWrapEl.classList.contains('open')) st.logEl.scrollTop = st.logEl.scrollHeight;
  }

  _setStatus(id, status) {
    const st = this.stages.get(id);
    if (!st) return;
    st.status = status;
    st.el.className = `stage stage--${status}`;
  }

  setStageActive(id) {
    for (let i = 1; i < id; i++) {
      const s = this.stages.get(i);
      if (s && s.status === 'pending') {
        this._setStatus(i, 'done');
        s.dotEl.textContent = '✓';
        s.timeEl.textContent = '';
      }
    }
    this._setStatus(id, 'active');
    this.currentStageId = id;
    this.stages.get(id).msgEl.textContent = '处理中…';
    this._showAction(id, 'cancel');
  }

  setStageDone(id, durationS, meta) {
    this._setStatus(id, 'done');
    const st = this.stages.get(id);
    st.dotEl.textContent = '✓';
    const parts = [];
    if (durationS != null) parts.push(formatElapsed(durationS));
    if (meta) parts.push(meta);
    st.timeEl.textContent = parts.join(' · ');
    st.barEl.hidden = true;
    this._clearActions(id);
  }

  setStageReused(id) {
    this._setStatus(id, 'reused');
    const st = this.stages.get(id);
    st.dotEl.textContent = '⚡';
    st.timeEl.innerHTML = '<span class="badge badge--reused">⚡ 缓存复用</span>';
    st.msgEl.textContent = '已命中共享素材缓存，快速跳过';
    this._clearActions(id);
  }

  setStageFailed(id, message) {
    this._setStatus(id, 'failed');
    const st = this.stages.get(id);
    st.dotEl.textContent = '✗';
    st.msgEl.textContent = message || '处理失败';
    st.msgEl.classList.add('err');
    st.barEl.hidden = true;
    this.addLogEntry(id, '❌ ' + (message || '未知错误'));
    st.logWrapEl.classList.add('open');
    this._showAction(id, 'retry');
  }

  setStageCancelled(id) {
    for (let i = id; i <= SSE_STAGES.length; i++) {
      const st = this.stages.get(i);
      if (!st) continue;
      this._setStatus(i, 'cancelled');
      st.dotEl.textContent = i === id ? '⏹' : '·';
      st.msgEl.textContent = i === id ? '已取消' : '';
      st.barEl.hidden = true;
      this._clearActions(i);
    }
  }

  setStageProgress(id, message, pct) {
    const st = this.stages.get(id);
    if (!st) return;
    if (message) st.msgEl.textContent = message;
    if (pct != null) {
      st.barEl.hidden = false;
      st.barFillEl.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    }
  }

  addLogEntry(id, message) {
    const st = this.stages.get(id || this.currentStageId);
    if (!st) return;
    const entry = document.createElement('div');
    entry.textContent = '› ' + message;
    st.logEl.appendChild(entry);
    if (st.logWrapEl.classList.contains('open')) st.logEl.scrollTop = st.logEl.scrollHeight;
    while (st.logEl.children.length > 50) st.logEl.firstChild.remove();
  }

  _showAction(id, type) {
    const st = this.stages.get(id);
    if (!st) return;
    st.actionsEl.innerHTML = '';
    const btn = document.createElement('button');
    btn.className = 'btn ' + (type === 'cancel' ? 'btn--danger btn--sm' : 'btn--secondary btn--sm');
    if (type === 'cancel') {
      btn.textContent = '⏹ 取消';
      btn.onclick = (e) => { e.stopPropagation(); this.hooks.onCancel && this.hooks.onCancel(id); };
    } else if (type === 'retry') {
      btn.textContent = '🔄 从此阶段重试';
      btn.onclick = (e) => { e.stopPropagation(); this.hooks.onRetry && this.hooks.onRetry(id); };
    }
    st.actionsEl.appendChild(btn);
  }

  _clearActions(id) {
    const st = this.stages.get(id);
    if (st) st.actionsEl.innerHTML = '';
  }

  clearAllActions() {
    this.stages.forEach((st) => { st.actionsEl.innerHTML = ''; });
  }

  updateFromPoll(stagesArray) {
    if (!stagesArray) return;
    stagesArray.forEach((s) => {
      const st = this.stages.get(s.stage_id);
      if (!st) return;
      switch (s.status) {
        case 'active':
          if (st.status !== 'active') this.setStageActive(s.stage_id);
          this.currentStageId = s.stage_id;
          if (s.meta) st.msgEl.textContent = s.meta;
          if (s.progress != null) this.setStageProgress(s.stage_id, s.meta, s.progress);
          break;
        case 'done':
          this.setStageDone(s.stage_id, s.duration_s, s.meta);
          break;
        case 'reused':
          this.setStageReused(s.stage_id);
          break;
        case 'failed':
          if (st.status !== 'failed') this.setStageFailed(s.stage_id, s.message);
          break;
        case 'cancelled':
          this._setStatus(s.stage_id, 'cancelled');
          this.stages.get(s.stage_id).dotEl.textContent = '·';
          this._clearActions(s.stage_id);
          break;
      }
    });
  }

  destroy() {
    this.container.innerHTML = '';
    this.stages.clear();
  }
}

export function formatElapsed(sec) {
  if (!sec && sec !== 0) return '';
  if (sec < 60) return `${Math.floor(sec)} 秒`;
  if (sec < 3600) {
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return s > 0 ? `${m}分${s}秒` : `${m}分钟`;
  }
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return m > 0 ? `${h}小时${m}分钟` : `${h}小时`;
}

export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = String(Math.floor(sec / 60)).padStart(2, '0');
  const s = String(sec % 60).padStart(2, '0');
  return `${m}:${s}`;
}

export function escHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}