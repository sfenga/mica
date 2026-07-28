import { store, api, apiPost, apiPatch, wsManager, toast } from '../app.js';
import { Badge, shortId, esc, EmptyState } from './common.js';

const STATUSES = ['backlog', 'todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'];

export function IssueCard(issue) {
  return {
    issue,
    shortId: shortId(issue.id),
    statusLabel: issue.status,
    assigneeLabel: issue.assignee_type ? `${issue.assignee_type}:${issue.assignee_id}` : '-',
    open() {
      store.selectedIssueId = issue.id;
      wsManager.connect(issue.id);
    },
  };
}

IssueCard.$template = `
  <div @click="open" class="bg-white border rounded-lg p-3 shadow-sm hover:shadow-md hover:border-indigo-300 cursor-pointer transition-all">
    <div class="flex items-center justify-between mb-1">
      <span class="text-xs font-mono text-gray-400">{{ shortId }}</span>
      <span v-scope="Badge(statusLabel)"></span>
    </div>
    <h4 class="text-sm font-medium text-gray-900 line-clamp-2">{{ issue.title }}</h4>
    <p class="text-xs text-gray-500 mt-1">{{ assigneeLabel }}</p>
    <p v-if="issue.project_name" class="text-xs text-indigo-600 mt-1">📁 {{ issue.project_name }}</p>
  </div>
`;

export function KanbanColumn(status) {
  return {
    status,
    label: status.replace(/_/g, ' '),
    get issues() { return store.issues.filter(i => i.status === status); },
  };
}

KanbanColumn.$template = `
  <div class="kanban-col flex-1 min-w-[260px] flex flex-col bg-gray-100 rounded-xl p-2">
    <div class="flex items-center justify-between mb-2 px-1">
      <span class="text-xs font-semibold uppercase tracking-wide text-gray-500">{{ label }}</span>
      <span class="text-xs font-mono text-gray-400">{{ issues.length }}</span>
    </div>
    <div class="space-y-2 overflow-y-auto flex-1">
      <div v-for="issue in issues" :key="issue.id" v-scope="IssueCard(issue)"></div>
      <EmptyState v-if="!issues.length" message="empty" />
    </div>
  </div>
`;

export function IssueDrawer() {
  return {
    store,
    get issue() { return store.issues.find(i => i.id === store.selectedIssueId); },
    close() { store.selectedIssueId = null; wsManager.disconnect(); },
    async transition(status) {
      await apiPatch(`/api/issues/${this.issue.id}/status`, { status }, `issue.status`);
      await this.refreshIssue();
    },
    async addComment(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      const text = f.get('text');
      if (!text) return;
      await apiPost(`/api/issues/${this.issue.id}/comments`, { text }, `issue.comment`);
      e.target.reset();
      await this.refreshIssue();
    },
    async rerun() {
      await apiPost(`/api/issues/${this.issue.id}/rerun`, {}, `issue.rerun`);
      toast('rerun queued', 'success');
    },
    async refreshIssue() {
      const updated = await api(`/api/issues/${store.selectedIssueId}`);
      updated.project_name = store.projects.find(p => p.id === updated.project_id)?.name || '';
      const idx = store.issues.findIndex(i => i.id === updated.id);
      if (idx >= 0) store.issues[idx] = updated;
    },
    getTransitions(status) {
      const map = {
        todo: [{ l: 'Start', s: 'in_progress' }],
        in_progress: [{ l: 'Submit review', s: 'in_review' }, { l: 'Done', s: 'done' }, { l: 'Block', s: 'blocked' }],
        in_review: [{ l: 'Approve → Done', s: 'done' }, { l: 'Request changes', s: 'in_progress' }, { l: 'Block', s: 'blocked' }],
        blocked: [{ l: 'Reopen', s: 'todo' }, { l: 'Cancel', s: 'cancelled' }],
        backlog: [{ l: 'Plan', s: 'todo' }],
      };
      return map[status] || [];
    },
    shortId,
    esc,
  };
}

IssueDrawer.$template = `
  <div v-if="store.selectedIssueId && issue" class="fixed inset-0 z-40 flex justify-end">
    <div class="absolute inset-0 bg-black/30" @click="close"></div>
    <div class="relative w-full max-w-2xl h-full bg-white shadow-2xl overflow-y-auto p-6 slide-in">
      <button @click="close" class="absolute top-4 right-4 text-gray-400 hover:text-gray-600">✕</button>
      <h2 class="text-xl font-semibold text-gray-900 pr-8">{{ issue.title }}</h2>
      <div class="flex items-center gap-3 mt-2">
        <span v-scope="Badge(issue.status)"></span>
        <span class="text-xs font-mono text-gray-500">{{ issue.assignee_type }}:{{ issue.assignee_id || '-' }}</span>
      </div>
      <p v-if="issue.workdir" class="text-xs font-mono text-gray-500 mt-2">📁 {{ issue.workdir }}</p>
      <div v-if="issue.body" class="mt-4 text-sm text-gray-700 prose prose-sm max-w-none">{{ issue.body }}</div>

      <div class="mt-6 flex flex-wrap gap-2">
        <button v-for="t in getTransitions(issue.status)" :key="t.s" @click="transition(t.s)" class="px-3 py-1.5 bg-white border border-gray-300 rounded-md text-xs hover:bg-gray-50">{{ t.l }}</button>
        <button @click="rerun" class="px-3 py-1.5 bg-white border border-gray-300 rounded-md text-xs hover:bg-gray-50">↻ Rerun</button>
      </div>

      <div class="mt-8">
        <h3 class="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-3">Activity</h3>
        <div v-for="t in issue.tasks || []" :key="t.id" class="text-xs font-mono text-gray-500 border-l-2 border-blue-400 pl-2 mb-2">
          task {{ shortId(t.id) }} → {{ t.status }}
        </div>
        <div v-for="(c, idx) in issue.comments || []" :key="issue.id + '-' + idx" class="text-sm bg-gray-50 rounded-lg p-3 mb-2" :class="c.startsWith('[agent') ? 'border-l-4 border-indigo-400' : 'border-l-4 border-blue-400'">
          {{ esc(c) }}
        </div>
      </div>

      <form @submit="addComment" class="mt-4 flex gap-2">
        <input name="text" placeholder="Add a comment..." class="flex-1 border rounded-md px-3 py-2 text-sm" required>
        <button class="px-3 py-1.5 bg-indigo-600 text-white text-sm font-medium rounded-md hover:bg-indigo-700 disabled:opacity-50" :disabled="store.isLoading('issue.comment')">Comment</button>
      </form>
    </div>
  </div>
  <div v-else></div>
`;

export function BoardView() {
  return {
    store,
    statuses: STATUSES,
    async load() {
      const [issues, projects, agents] = await Promise.all([
        api(`/api/issues?workspace_id=${encodeURIComponent(store.ws)}&member_id=${encodeURIComponent(store.member)}`),
        api('/api/projects'),
        api('/api/agents'),
      ]);
      store.projects = projects;
      store.agents = agents;
      // enrich issues with project_name
      store.issues = issues.map(i => ({
        ...i,
        project_name: projects.find(p => p.id === i.project_id)?.name || '',
      }));
    },
    async createIssue(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      await apiPost('/api/issues', {
        title: f.get('title'),
        body: f.get('body') || '',
        assignee_type: f.get('assignee_type'),
        assignee_id: f.get('assignee_id'),
        project_id: f.get('project_id') || '',
        workspace_id: store.ws,
        member_id: store.member,
      }, 'issue.create');
      e.target.reset();
      await this.load();
      toast('issue created', 'success');
    },
    mounted() {
      this.load();
      this.interval = setInterval(() => this.load(), 3000);
      this.stopWatchWs = this.$watch ? this.$watch(() => store.ws, () => this.load()) : null;
      this.stopWatchMember = this.$watch ? this.$watch(() => store.member, () => this.load()) : null;
    },
    unmounted() {
      clearInterval(this.interval);
      if (this.stopWatchWs) this.stopWatchWs();
      if (this.stopWatchMember) this.stopWatchMember();
    },
  };
}

BoardView.$template = `
  <div v-scope="BoardView()" @vue:mounted="mounted" @vue:unmounted="unmounted" class="h-full flex flex-col">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold">Issue Board</h2>
      <button @click="load" class="text-xs text-indigo-600 hover:underline">refresh</button>
    </div>
    <form @submit="createIssue" class="flex gap-2 mb-4 flex-wrap">
      <input name="title" placeholder="Issue title" class="border rounded-md px-3 py-1.5 text-sm" required>
      <input name="body" placeholder="Body (optional)" class="border rounded-md px-3 py-1.5 text-sm w-64">
      <select name="assignee_type" class="border rounded-md px-3 py-1.5 text-sm">
        <option value="agent">agent</option>
        <option value="squad">squad</option>
        <option value="member">member</option>
      </select>
      <select name="assignee_id" class="border rounded-md px-3 py-1.5 text-sm">
        <option v-for="a in store.agents" :key="a.id" :value="a.id">{{ a.name }} ({{ a.id }})</option>
      </select>
      <select name="project_id" class="border rounded-md px-3 py-1.5 text-sm">
        <option value="">no project</option>
        <option v-for="p in store.projects" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select>
      <button class="btn-primary text-sm" :disabled="store.isLoading('issue.create')">+ New</button>
    </form>
    <div class="flex-1 overflow-x-auto">
      <div class="flex gap-4 h-full min-w-max pb-2">
        <div v-for="s in statuses" :key="s" v-scope="KanbanColumn(s)"></div>
      </div>
    </div>
    <div v-scope="IssueDrawer()"></div>
  </div>
`;
