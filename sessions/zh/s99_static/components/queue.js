import { store, api } from '../app.js';
import { Badge, shortId, EmptyState } from './common.js';

export function TracePanel(taskId) {
  return {
    taskId,
    task: null,
    async load() {
      this.task = await api(`/api/tasks/${this.taskId}`);
    },
    mounted() { this.load(); },
    iconFor(e) {
      if (e.type === 'claim') return '📌';
      if (e.type === 'progress') return '⏳';
      if (e.type === 'done') return '✅';
      if (e.type === 'fail') return '❌';
      if (e.type === 'result') return '🏁';
      if (e.kind === 'text') return '💬';
      if (e.kind === 'thinking') return '💭';
      if (e.kind === 'tool_use') return '🔧';
      return '·';
    },
  };
}

TracePanel.$template = `
  <div v-if="task" class="mt-4 border rounded-lg p-4 bg-white">
    <div class="flex items-center justify-between mb-3">
      <h3 class="font-semibold text-sm">Trace: {{ shortId(task.id) }}</h3>
      <span v-scope="Badge(task.status)"></span>
    </div>
    <div v-if="!task.events || !task.events.length" class="text-sm text-gray-500">No events recorded.</div>
    <div v-else class="space-y-2 max-h-96 overflow-y-auto">
      <div v-for="e in task.events" :key="e.ts + (e.type || e.kind)" class="text-xs font-mono border-l-4 pl-2 py-1"
           :class="e.type === 'fail' || e.kind === 'tool_result' && e.is_error ? 'border-red-400 bg-red-50' : 'border-gray-300'">
        <span class="mr-2">{{ iconFor(e) }}</span>
        <span class="text-gray-600">{{ e.type || e.kind }}</span>
        <span v-if="e.message" class="ml-2 text-gray-800">{{ e.message }}</span>
        <span v-if="e.text" class="ml-2 text-gray-800">{{ e.text }}</span>
        <span v-if="e.output" class="ml-2 text-gray-800">{{ String(e.output).slice(0, 200) }}</span>
      </div>
    </div>
  </div>
  <div v-else class="text-sm text-gray-500">Loading trace...</div>
`;

export function TaskTable() {
  return {
    store,
    filter: '',
    selectTask(t) { store.selectedTaskId = t.id; },
    shortId,
    get filteredTasks() {
      if (!this.filter) return store.tasks;
      return store.tasks.filter(t => t.status.includes(this.filter));
    },
  };
}

TaskTable.$template = `
  <div>
    <div class="flex items-center gap-2 mb-3">
      <input v-model="filter" placeholder="Filter by status..." class="border rounded-md px-3 py-1.5 text-sm">
      <button @click="store.tab = 'queue'" class="text-xs text-indigo-600 hover:underline">refresh</button>
    </div>
    <table class="min-w-full text-sm">
      <thead class="bg-gray-100 text-gray-600 font-medium">
        <tr>
          <th class="px-3 py-2 text-left">ID</th>
          <th class="px-3 py-2 text-left">Status</th>
          <th class="px-3 py-2 text-left">Issue</th>
          <th class="px-3 py-2 text-left">Agent</th>
          <th class="px-3 py-2 text-left">Session</th>
          <th class="px-3 py-2 text-left">Error</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in filteredTasks" :key="t.id" @click="selectTask(t)"
            :class="store.selectedTaskId === t.id ? 'bg-indigo-50' : 'hover:bg-gray-50'"
            class="cursor-pointer border-b">
          <td class="px-3 py-2 font-mono">{{ shortId(t.id) }}</td>
          <td class="px-3 py-2"><span v-scope="Badge(t.status)"></span></td>
          <td class="px-3 py-2 font-mono">{{ shortId(t.issue_id) }}</td>
          <td class="px-3 py-2">{{ t.assignee_id || '-' }}</td>
          <td class="px-3 py-2 font-mono">{{ shortId(t.session_id) }}</td>
          <td class="px-3 py-2 text-red-600 truncate max-w-xs">{{ t.error || '' }}</td>
        </tr>
        <tr v-if="!filteredTasks.length"><td colspan="6"><EmptyState message="no tasks" /></td></tr>
      </tbody>
    </table>
  </div>
`;

export function QueueView() {
  return {
    store,
    async load() {
      store.tasks = await api(`/api/tasks?workspace_id=${encodeURIComponent(store.ws)}`);
    },
    mounted() { this.load(); this.interval = setInterval(() => this.load(), 3000); this.stopWatchWs = this.$watch ? this.$watch(() => store.ws, () => this.load()) : null; },
    unmounted() { clearInterval(this.interval); if (this.stopWatchWs) this.stopWatchWs(); },
  };
}

QueueView.$template = `
  <div v-scope="QueueView()" @vue:mounted="mounted" @vue:unmounted="unmounted" class="h-full flex flex-col">
    <h2 class="text-lg font-semibold mb-3">Task Queue</h2>
    <div class="flex-1 overflow-auto">
      <div v-scope="TaskTable()"></div>
      <div v-if="store.selectedTaskId" v-scope="TracePanel(store.selectedTaskId)"></div>
    </div>
  </div>
`;
