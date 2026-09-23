/* ============================================================
   config.js — 配置收集 / 持久化（读取时兼容旧键名，老用户无感迁移）
   ============================================================ */

export const CONFIG_STORAGE_KEY = 'v2l_savedConfig_v1';

export const MODES = {
  podcast: { key: 'podcast', label: '播客版', full: '中文播客版', icon: '🎙️', badge: 'badge--podcast' },
  faithful: { key: 'faithful', label: '忠实版', full: '忠实翻译版', icon: '📑', badge: 'badge--faithful' },
  condensed: { key: 'condensed', label: '浓缩版', full: '精华浓缩版', icon: '⚡', badge: 'badge--condensed' },
};

export const SSE_STAGES = [
  { id: 1, name: '下载', icon: '📥' },
  { id: 2, name: '转写', icon: '🎙️' },
  { id: 3, name: '清洗', icon: '🧹' },
  { id: 4, name: '翻译', icon: '🌐' },
  { id: 5, name: 'TTS', icon: '🔊' },
  { id: 6, name: '合并', icon: '🎵' },
];

export function setSelectValue(select, value) {
  if (!value) return;
  if (![...select.options].some((option) => option.value === value)) {
    select.add(new Option(value, value));
  }
  select.value = value;
}

export function collectConfig() {
  const speedEl = document.getElementById('ttsSpeed');
  const speedVal = speedEl ? parseFloat(speedEl.value) : 1.0;
  return {
    llmApiKey: document.getElementById('llmKey').value,
    llmBaseUrl: document.getElementById('llmBaseUrl').value,
    model: document.getElementById('llmModel').value,
    ttsApiKey: document.getElementById('ttsKey').value,
    ttsBaseUrl: document.getElementById('ttsBaseUrl').value,
    ttsModel: document.getElementById('ttsModel').value,
    ttsVoice: document.getElementById('ttsVoice').value,
    ttsSpeed: Number.isFinite(speedVal) ? speedVal : 1.0,
    mode: document.querySelector('.mode-card.is-selected')?.dataset.mode || 'podcast',
  };
}

export function saveConfig() {
  const remember = document.getElementById('rememberSwitch');
  if (!remember.checked) return;
  try { localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(collectConfig())); } catch { /* 浏览器可能禁用存储 */ }
}

export function restoreConfig() {
  try {
    const raw = localStorage.getItem(CONFIG_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    document.getElementById('rememberSwitch').checked = true;
    document.getElementById('llmKey').value = saved.llmApiKey || '';
    document.getElementById('llmBaseUrl').value = saved.llmBaseUrl || 'https://api.deepseek.com';
    setSelectValue(document.getElementById('llmModel'), saved.model);
    document.getElementById('ttsKey').value = saved.ttsApiKey || '';
    document.getElementById('ttsBaseUrl').value = saved.ttsBaseUrl || 'https://api.fish.audio/v1';
    let savedVoice = saved.ttsVoice || '7f92f8afb8ec43bf81429cc1c9199cb1';
    if (['苏打', '白桦', '冰糖', '茉莉', 'Mia', 'Chloe', 'Milo', 'Dean'].includes(savedVoice)) {
      savedVoice = '7f92f8afb8ec43bf81429cc1c9199cb1';
    }
    setSelectValue(document.getElementById('ttsVoice'), savedVoice);
    const speedEl = document.getElementById('ttsSpeed');
    const speedValEl = document.getElementById('ttsSpeedVal');
    if (speedEl) {
      const savedSpeed = parseFloat(saved.ttsSpeed);
      const val = Number.isFinite(savedSpeed) && savedSpeed >= 0.5 && savedSpeed <= 2.0 ? savedSpeed : 1.0;
      speedEl.value = val;
      if (speedValEl) speedValEl.textContent = `${val.toFixed(val % 1 === 0 ? 1 : 2)}x`;
    }
    const savedMode = ['podcast', 'faithful', 'condensed'].includes(saved.mode) ? saved.mode : 'podcast';
    const card = document.querySelector(`.mode-card[data-mode="${savedMode}"]`);
    if (card) {
      document.querySelectorAll('.mode-card').forEach((c) => {
        const on = c === card;
        c.classList.toggle('is-selected', on);
        c.setAttribute('aria-checked', String(on));
      });
    }
  } catch {
    localStorage.removeItem(CONFIG_STORAGE_KEY);
  }
}

export function clearConfig() {
  localStorage.removeItem(CONFIG_STORAGE_KEY);
  document.getElementById('rememberSwitch').checked = false;
}

/** 校验 LLM 配置是否就绪，返回缺失字段名列表 */
export function missingConfigFields() {
  const errors = [];
  if (!document.getElementById('llmKey').value.trim()) errors.push('llmKeyField');
  if (!document.getElementById('llmModel').value) errors.push('llmModel');
  return errors;
}