/* ============================================================
   store.js — 全局状态机 + 极简 pub/sub 事件总线
   视图切换、任务阶段、连接状态都收敛于此，组件通过
   getState/setState 读写，通过 on/emit 跨模块通信。
   ============================================================ */

const state = {
  view: 'submit',                 // submit | progress | result | error
  phase: 'idle',                  // idle | validateting | preflight | processing | done | failed | cancelled
  task: null,                     // { videoId, mode, startedAt }
  preflight: null,                // { kind: conflict|variant_exists|reuse_available, data }
  connection: 'none',             // none | sse | polling | offline
};

const listeners = new Set();
const busListeners = new Map(); // event -> Set<fn>

export const store = {
  getState() { return state; },
  setState(patch) {
    Object.assign(state, patch);
    listeners.forEach((fn) => fn(state));
  },
  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },

  on(event, fn) {
    if (!busListeners.has(event)) busListeners.set(event, new Set());
    busListeners.get(event).add(fn);
  },
  emit(event, payload) {
    const set = busListeners.get(event);
    if (set) set.forEach((fn) => { try { fn(payload); } catch (e) { console.error(e); } });
  },
};