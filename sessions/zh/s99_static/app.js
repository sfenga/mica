import { createApp, reactive } from 'https://unpkg.com/petite-vue@0.4.1/dist/petite-vue.es.js';
import { Badge, EmptyState, Spinner, ToastContainer, shortId, esc } from './components/common.js';
import { BoardView, KanbanColumn, IssueCard, IssueDrawer } from './components/board.js';
import { QueueView, TaskTable, TracePanel } from './components/queue.js';
import { AgentsView, AgentForm, AgentCard } from './components/agents.js';
import { ProjectsView } from './components/projects.js';
import { SquadsView } from './components/squads.js';
import { AutopilotView } from './components/autopilot.js';
import { SystemView } from './components/system.js';

export const store = reactive({
  tab: 'board',
  ws: 'ws-1',
  member: 'alice',
  wsConn: { connected: false, issueId: null },
  daemon: {
    running: false,
    queued: 0,
    claimed_count: 0,
    completed_count: 0,
    poll_interval: null,
    slots: { configured: false, max_slots: 0, available: 0, occupied: [] },
  },
  issues: [],
  tasks: [],
  agents: [],
  projects: [],
  squads: [],
  skills: [],
  autopilots: [],
  runtimes: [],
  workspaces: [],
  selectedIssueId: null,
  selectedTaskId: null,
  toasts: [],
  loading: {},
  errors: {},
  events: [],
  projectPath: '',
  showDirPicker: false,
  isLoading(key) { return !!this.loading[key]; },
  isError(key) { return this.errors[key] || ''; },
});

export const tabs = [
  { key: 'board', label: 'Board' },
  { key: 'queue', label: 'Queue' },
  { key: 'agents', label: 'Agents' },
  { key: 'projects', label: 'Projects' },
  { key: 'squads', label: 'Squads' },
  { key: 'autopilot', label: 'Autopilot' },
  { key: 'system', label: 'System' },
];

export function toast(message, type = 'info') {
  const id = Date.now() + Math.random();
  store.toasts.push({ id, message, type });
  setTimeout(() => {
    store.toasts = store.toasts.filter(t => t.id !== id);
  }, 3000);
}

export async function api(path, opts = {}) {
  const { loadingKey: key, ...rest } = opts;
  if (key) store.loading[key] = true;
  try {
    const res = await fetch(path, rest);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (data && data.error) throw new Error(data.error);
    if (key) delete store.errors[key];
    return data;
  } catch (err) {
    if (key) store.errors[key] = err.message;
    toast(err.message, 'error');
    throw err;
  } finally {
    if (key) store.loading[key] = false;
  }
}

export function apiPost(path, params = {}, loadingKey = null) {
  const qs = new URLSearchParams(params).toString();
  return api(`${path}?${qs}`, { method: 'POST', loadingKey });
}

export function apiPatch(path, params = {}, loadingKey = null) {
  const qs = new URLSearchParams(params).toString();
  return api(`${path}?${qs}`, { method: 'PATCH', loadingKey });
}

export function apiDelete(path, loadingKey = null) {
  return api(path, { method: 'DELETE', loadingKey });
}

export class WsManager {
  constructor() {
    this.socket = null;
    this.reconnectTimer = null;
    this.backoff = 3000;
  }

  connect(issueId) {
    if (this.socket && store.wsConn.issueId === issueId) return;
    this.disconnect();
    store.wsConn.issueId = issueId;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws/issues/${issueId}`;
    try {
      this.socket = new WebSocket(url);
    } catch (e) {
      store.wsConn.connected = false;
      this.scheduleReconnect(issueId);
      return;
    }
    this.socket.onopen = () => {
      store.wsConn.connected = true;
      this.backoff = 3000;
    };
    this.socket.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { data = { raw: ev.data }; }
      store.events.unshift({ ts: new Date().toISOString(), type: data.type || 'event', data });
      if (store.events.length > 50) store.events.pop();
      if (store.selectedIssueId === issueId) {
        // Views should refresh via polling or explicit handler
        window.dispatchEvent(new CustomEvent('issue-event', { detail: data }));
      }
    };
    this.socket.onclose = () => {
      store.wsConn.connected = false;
      this.scheduleReconnect(issueId);
    };
    this.socket.onerror = () => {
      store.wsConn.connected = false;
    };
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
    store.wsConn.issueId = null;
    store.wsConn.connected = false;
  }

  scheduleReconnect(issueId) {
    this.reconnectTimer = setTimeout(() => {
      this.connect(issueId);
    }, this.backoff);
    this.backoff = Math.min(this.backoff * 2, 30000);
  }
}

export const wsManager = new WsManager();

export function loadDaemonStatus() {
  return api('/api/daemon/status').then(d => { Object.assign(store.daemon, d); return d; });
}

export const daemonActions = {
  async start() { await apiPost('/api/daemon/start', {}, 'daemon.start'); await loadDaemonStatus(); toast('daemon started', 'success'); },
  async stop() { await apiPost('/api/daemon/stop', {}, 'daemon.stop'); await loadDaemonStatus(); toast('daemon stopped', 'warning'); },
  async runOnce() { const d = await apiPost('/api/daemon/run-once', {}, 'daemon.run'); await loadDaemonStatus(); toast(`ran ${d.run} task(s)`, 'success'); },
};

export const demoActions = {
  async seed() { const d = await apiPost('/api/demo/seed', {}, 'demo.seed'); toast(`seeded ${d.issues} issues`, 'success'); },
  async reset() { if (!confirm('Reset all demo data?')) return; await apiPost('/api/demo/reset', {}, 'demo.reset'); toast('reset complete', 'warning'); },
};

export const autopilotActions = {
  async tick() { const d = await apiPost('/api/autopilots/tick', {}, 'autopilot.tick'); toast(`autopilot tick: ${d.triggered}`, 'success'); },
};

function App() {
  return {
    store,
    tabs,
    ToastContainer,
    get currentTabLabel() {
      const t = tabs.find(x => x.key === store.tab);
      return t ? t.label : 'Board';
    },
    startDaemon: daemonActions.start,
    stopDaemon: daemonActions.stop,
    runDaemonOnce: daemonActions.runOnce,
    seedDemo: demoActions.seed,
    resetDemo: demoActions.reset,
    autopilotTick: autopilotActions.tick,
    loadWorkspaces() { api('/api/workspaces').then(d => store.workspaces = d); },
  };
}

createApp({
  App,
  Badge, EmptyState, Spinner, ToastContainer,
  BoardView, KanbanColumn, IssueCard, IssueDrawer,
  QueueView, TaskTable, TracePanel,
  AgentsView, AgentForm, AgentCard,
  ProjectsView,
  SquadsView,
  AutopilotView,
  SystemView,
  shortId, esc,
}).mount('#app');
