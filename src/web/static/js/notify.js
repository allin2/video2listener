/* ============================================================
   notify.js — 页面标题状态 + 系统桌面通知
   ============================================================ */

const BASE_TITLE = 'video2listener · YouTube 转中文播客';

export function titleIdle() { document.title = BASE_TITLE; }
export function titleProgress() { document.title = '[●●●] video2listener'; }
export function titleDone(title) {
  document.title = title ? `[✓] ${title} · video2listener` : '[✓] video2listener';
}
export function titleFailed() { document.title = '[✗] video2listener'; }

export function requestNotificationPermission() {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') Notification.requestPermission();
}

export function fireNotification(title, vid) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try {
    const n = new Notification('✅ 视频已生成', { body: `《${title}》已生成，点击查看`, tag: vid });
    n.onclick = () => { window.focus(); n.close(); };
  } catch { /* 通知可能被浏览器拦截 */ }
}