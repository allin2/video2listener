/* ============================================================
   modals.js — 决策弹窗（409 / variant_exists / reuse_available）、
   通用确认弹窗、Toast 通知
   ============================================================ */

export function toast(msg, type = 'info', timeout = 3200) {
  const root = document.getElementById('toastRoot');
  const t = document.createElement('div');
  t.className = `toast toast--${type}`;
  const icon = { success: '✅', error: '❌', warn: '⚠️', info: 'ℹ️' }[type] || 'ℹ️';
  t.innerHTML = `<span>${icon}</span><span>${msg}</span>`;
  root.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 260);
  }, timeout);
}

function openModal(id) {
  const m = document.getElementById(id);
  m.hidden = false;
  requestAnimationFrame(() => m.classList.add('is-open'));
}

function closeModal(id) {
  const m = document.getElementById(id);
  m.classList.remove('is-open');
  setTimeout(() => { m.hidden = true; }, 260);
}

/** 决策弹窗：动态标题/描述/动作按钮 */
export function showDecision({ icon, title, desc, actions }) {
  document.getElementById('dmIcon').textContent = icon;
  document.getElementById('dmTitle').textContent = title;
  document.getElementById('dmDesc').textContent = desc;
  const box = document.getElementById('dmActions');
  box.innerHTML = '';
  actions.forEach((a, i) => {
    const b = document.createElement('button');
    b.className = `btn ${a.cls || 'btn--secondary'}`;
    b.textContent = a.label;
    if (i === 0) b.classList.add('dm-primary');
    b.onclick = () => { closeModal('decisionModal'); a.fn && a.fn(); };
    box.appendChild(b);
  });
  openModal('decisionModal');
}

export function closeDecision() { closeModal('decisionModal'); }
export function closeConfirm() { closeModal('confirmModal'); }

/**
 * 通用二次确认弹窗
 * @returns {Promise<boolean>} 用户点“确认”则 resolve(true)
 */
export function confirmBox({ icon, title, desc, okText = '确认', danger = true }) {
  return new Promise((resolve) => {
    document.getElementById('cfIcon').textContent = icon;
    document.getElementById('cfTitle').textContent = title;
    document.getElementById('cfDesc').textContent = desc;
    const ok = document.getElementById('cfOk');
    ok.textContent = okText;
    ok.className = 'btn ' + (danger ? 'btn--danger' : 'btn--primary');
    const done = (val) => {
      closeModal('confirmModal');
      ok.onclick = null;
      cancelBtn.onclick = null;
      overlay.onclick = null;
      resolve(val);
    };
    const cancelBtn = document.getElementById('cfCancel');
    const overlay = document.getElementById('cfOverlay');
    ok.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
    overlay.onclick = () => done(false);
    openModal('confirmModal');
  });
}

/** Esc / 遮罩关闭决策弹窗 */
export function bindModalEsc() {
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeModal('decisionModal');
      closeModal('confirmModal');
    }
  });
}