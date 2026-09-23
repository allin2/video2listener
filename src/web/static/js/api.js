/* ============================================================
   api.js — 全部后端 HTTP 调用的唯一封装（stream.js 的 SSE 除外）
   ============================================================ */

async function req(url, options = {}) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => null);
  return { ok: resp.ok, status: resp.status, data };
}

export function apiProcess(payload) {
  return req('/api/process', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function apiStatus(vid, mode) {
  return req(`/api/status/${vid}/${mode}`);
}

export function apiTasks() {
  return req('/api/tasks');
}

export function apiModels(apiKey, baseUrl) {
  return req('/api/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl }),
  });
}

export function apiTtsVoices(apiKey, baseUrl) {
  return req('/api/tts-voices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl }),
  });
}

export function apiTtsPreview(payload) {
  return fetch('/api/tts-preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function apiCancel(vid, mode) {
  return fetch(`/api/cancel/${vid}/${mode}`, { method: 'POST' });
}

export function apiDeleteVariant(vid, mode) {
  return req(`/api/tasks/${vid}/${mode}`, { method: 'DELETE' });
}

export function apiDeleteTask(vid) {
  return req(`/api/tasks/${vid}`, { method: 'DELETE' });
}

export function apiVariantText(vid, mode, kind) {
  return fetch(`/api/tasks/${vid}/${mode}/text?kind=${kind}`);
}

/** 下载/播放 URL（分卷时带 part 参数） */
export function downloadURL(vid, mode, part) {
  return part ? `/api/download/${vid}/${mode}?part=${encodeURIComponent(part)}` : `/api/download/${vid}/${mode}`;
}

/** SSE 事件流 URL */
export function streamURL(vid, mode) {
  return `/api/status/${vid}/${mode}/stream`;
}