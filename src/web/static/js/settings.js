/* ============================================================
   settings.js — 设置抽屉：密钥显隐/复制、查询模型、加载音色、
   TTS 试听、配置记忆（localStorage 键不改变）
   ============================================================ */

import { CONFIG_STORAGE_KEY, clearConfig, collectConfig, saveConfig, setSelectValue, restoreConfig } from './config.js';
import { apiModels, apiTtsVoices, apiTtsPreview } from './api.js';
import { toast } from './modals.js';

const $ = (id) => document.getElementById(id);

/** 显示/隐藏密钥输入框 */
function bindKeyToggle(btnId, inputId) {
  $(btnId).addEventListener('click', () => {
    const input = $(inputId);
    const isPass = input.type === 'password';
    input.type = isPass ? 'text' : 'password';
    $(btnId).textContent = isPass ? '🙈' : '👁️';
  });
}

/** 复制密钥到剪贴板 */
function bindKeyCopy(btnId, inputId) {
  $(btnId).addEventListener('click', async () => {
    const input = $(inputId);
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(input.value);
      } else {
        const ta = document.createElement('textarea');
        ta.value = input.value;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast('API Key 已复制', 'success');
    } catch { /* 剪贴板被拒绝 */ }
  });
}

/** 查询 LLM 模型 → 填充下拉 */
async function fetchModels() {
  const apiKey = $('llmKey').value.trim();
  const baseUrl = $('llmBaseUrl').value.trim() || 'https://api.deepseek.com';
  const preferredModel = $('llmModel').value;
  if (!apiKey) {
    toast('请先输入 LLM API Key', 'warn');
    return;
  }
  const btn = $('fetchModelsBtn');
  btn.disabled = true;
  btn.textContent = '查询中…';
  $('llmModel').innerHTML = '<option value="">查询中…</option>';
  try {
    const { ok, status, data } = await apiModels(apiKey, baseUrl);
    if (!ok) {
      $('llmModel').innerHTML = `<option value="">❌ ${(data && (data.error || data.message)) || '查询失败'}</option>`;
    } else if (!(data.models || []).length) {
      $('llmModel').innerHTML = '<option value="">未找到模型</option>';
    } else {
      $('llmModel').innerHTML = data.models.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)}</option>`).join('');
      setSelectValue($('llmModel'), preferredModel);
      saveConfig();
      toast(`已拉取 ${data.models.length} 个可用模型`, 'success');
    }
  } catch {
    $('llmModel').innerHTML = '<option value="">❌ 网络错误</option>';
  } finally {
    btn.disabled = false;
    btn.textContent = '查询模型';
  }
}

/** 加载官方音色 → 填充下拉 */
async function fetchVoices() {
  const apiKey = $('ttsKey').value.trim();
  const baseUrl = $('ttsBaseUrl').value.trim() || 'https://api.fish.audio/v1';
  const preferredVoice = $('ttsVoice').value || '7f92f8afb8ec43bf81429cc1c9199cb1';
  const btn = $('ttsVoicesBtn');
  btn.disabled = true;
  btn.textContent = '查询中…';
  $('ttsVoice').innerHTML = '<option value="">查询中…</option>';
  try {
    const { ok, status, data } = await apiTtsVoices(apiKey, baseUrl);
    if (!ok) {
      $('ttsVoice').innerHTML = `<option value="">❌ ${(data && (data.error || data.message)) || '查询失败'}</option>`;
    } else {
      const voices = data.voices || [];
      if (!voices.length) {
        $('ttsVoice').innerHTML = '<option value="">未找到音色</option>';
      } else {
        $('ttsVoice').innerHTML = voices
          .map((v) => `<option value="${escapeHtml(v.id)}"${v.id === preferredVoice ? ' selected' : ''}>${escapeHtml(v.name || v.id)}</option>`)
          .join('');
        if (![...$('ttsVoice').options].some((o) => o.value === preferredVoice)) $('ttsVoice').value = '7f92f8afb8ec43bf81429cc1c9199cb1';
        saveConfig();
      }
    }
  } catch {
    $('ttsVoice').innerHTML = '<option value="">❌ 网络错误</option>';
  } finally {
    btn.disabled = false;
    btn.textContent = '重置预置音色';
  }
}

/** 预置本地音色（Fish Audio 默认 + Edge-TTS 常用） */
function loadPresetVoices() {
  const voices = [
    { id: '7f92f8afb8ec43bf81429cc1c9199cb1', name: '御姐 · 中文女声（Fish Audio）' },
    { id: '5c353fdb312f4888836a9a5680099ef0', name: '女大 · 中文女声（Fish Audio）' },
    { id: 'zh-CN-XiaoxiaoNeural', name: '晓晓 · 中文女声（Edge-TTS 免Key）' },
    { id: 'zh-CN-YunxiNeural', name: '云希 · 中文男声（Edge-TTS 免Key）' },
    { id: '__custom__', name: '✏️ 输入自定义 Reference ID…' },
  ];
  $('ttsVoice').innerHTML = voices
    .map((v) => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`)
    .join('');
}

/** 🔊 音色试听 */
let _previewAudio = null;
async function previewTts() {
  if (_previewAudio) { _previewAudio.pause(); _previewAudio = null; }
  const btn = $('ttsPreviewBtn');
  btn.disabled = true;
  btn.textContent = '播放中…';

  const apiKey = $('ttsKey').value.trim();
  const baseUrl = $('ttsBaseUrl').value.trim() || (apiKey ? 'https://api.fish.audio/v1' : '');
  const provider = apiKey ? 'fish' : 'edge';
  const voice = $('ttsVoice').value || (apiKey ? '7f92f8afb8ec43bf81429cc1c9199cb1' : 'zh-CN-XiaoxiaoNeural');
  const model = $('ttsModel').value.trim() || (apiKey ? 's2.1-pro-free' : '');
  const speed = parseFloat($('ttsSpeed')?.value) || 1.0;

  try {
    const resp = await apiTtsPreview({ provider, voice, api_key: apiKey, base_url: baseUrl, model, speed });
    if (!resp.ok) {
      const err = await resp.json().catch(() => null);
      throw new Error((err && err.error) || 'TTS 试听失败');
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    _previewAudio = new Audio(url);
    _previewAudio.onended = resetPreviewBtn;
    _previewAudio.onerror = () => { resetPreviewBtn('试听失败'); };
    _previewAudio.play().catch(() => resetPreviewBtn('试听失败'));
  } catch (err) {
    toast(err.message || 'TTS 试听失败', 'error');
    resetPreviewBtn('试听失败');
  }
}

function resetPreviewBtn(fallback) {
  const btn = $('ttsPreviewBtn');
  btn.disabled = false;
  btn.textContent = fallback || '🔊 试听';
  _previewAudio = null;
  if (fallback) setTimeout(() => { if (btn.textContent === fallback) btn.textContent = '🔊 试听'; }, 2000);
}

/** 记忆配置开关 */
function bindRemember() {
  const remember = $('rememberSwitch');
  remember.addEventListener('change', () => {
    if (remember.checked) saveConfig();
    else localStorage.removeItem(CONFIG_STORAGE_KEY);
  });
  $('clearCfgBtn').addEventListener('click', () => {
    clearConfig();
    toast('已清除本地保存的配置', 'success');
    const b = $('clearCfgBtn');
    b.textContent = '已清除';
    setTimeout(() => { b.textContent = '清除已保存配置'; }, 1200);
  });
  // 配置区任意输入变更 → 若开启“记住”则保存
  const body = document.getElementById('settingsDrawer');
  ['input', 'change'].forEach((evt) =>
    body.addEventListener(evt, (e) => {
      if (['INPUT', 'SELECT'].includes(e.target.tagName)) saveConfig();
    })
  );
}

function escapeHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

/** 初始化设置抽屉（供 main.js 调用一次） */
export function initSettings() {
  restoreConfig();
  loadPresetVoices();
  const saved = (() => { try { return JSON.parse(localStorage.getItem(CONFIG_STORAGE_KEY)); } catch { return null; } })();
  let preferred = (saved && saved.ttsVoice) || '7f92f8afb8ec43bf81429cc1c9199cb1';
  if (['苏打', '白桦', '冰糖', '茉莉', 'Mia', 'Chloe', 'Milo', 'Dean'].includes(preferred)) {
    preferred = '7f92f8afb8ec43bf81429cc1c9199cb1';
  }
  setSelectValue($('ttsVoice'), preferred);

  bindKeyToggle('keyToggle', 'llmKey');
  bindKeyCopy('keyCopy', 'llmKey');
  bindKeyToggle('ttsKeyToggle', 'ttsKey');

  $('fetchModelsBtn').addEventListener('click', fetchModels);
  $('ttsVoicesBtn').addEventListener('click', fetchVoices);
  $('ttsPreviewBtn').addEventListener('click', previewTts);
  const speedSlider = $('ttsSpeed');
  const speedLabel = $('ttsSpeedVal');
  if (speedSlider && speedLabel) {
    speedSlider.addEventListener('input', () => {
      const v = parseFloat(speedSlider.value) || 1.0;
      speedLabel.textContent = `${v.toFixed(v % 1 === 0 ? 1 : 2)}x`;
      saveConfig();
    });
  }
  $('ttsVoice').addEventListener('change', () => {
    if ($('ttsVoice').value === '__custom__') {
      const customId = prompt('请输入 Fish Audio 的 32 位 Reference ID / 音色 ID:');
      if (customId && customId.trim()) {
        setSelectValue($('ttsVoice'), customId.trim());
      } else {
        $('ttsVoice').value = '7f92f8afb8ec43bf81429cc1c9199cb1';
      }
    }
    saveConfig();
  });
  bindRemember();

  // 打开抽屉时回填当前配置中的 voice（若用户已选过）
  $('settingsBtn').addEventListener('click', () => {
    const cfg = collectConfig();
    setSelectValue($('ttsVoice'), cfg.ttsVoice || 'zh-CN-XiaoxiaoNeural');
    setSelectValue($('llmModel'), cfg.model);
  });
}