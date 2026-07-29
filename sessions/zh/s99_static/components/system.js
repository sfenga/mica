import { store, api, apiPost, loadDaemonStatus, daemonActions } from '../app.js';

const DaemonControlsTemplate = `
  <div class="flex flex-wrap gap-2">
    <button @click="start" class="btn-primary" :disabled="store.daemon.running">▶ Start</button>
    <button @click="stop" class="btn-secondary" :disabled="!store.daemon.running">■ Stop</button>
    <button @click="runOnce" class="btn-secondary">Run once</button>
  </div>
`;

export function DaemonControls() {
  return {
    store,
    start: daemonActions.start,
    stop: daemonActions.stop,
    runOnce: daemonActions.runOnce,
    $template: DaemonControlsTemplate,
  };
}

const SystemViewTemplate = `
  <div @vue:mounted="mounted" class="h-full overflow-auto space-y-6">
    <section class="bg-white border rounded-lg p-4">
      <h2 class="text-lg font-semibold mb-3">Daemon</h2>
      <div class="grid grid-cols-2 md:grid-cols-6 gap-4 mb-4">
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Status</div><div class="font-semibold" :class="store.daemon.running ? 'text-emerald-600' : 'text-gray-600'">{{ store.daemon.running ? 'running' : 'stopped' }}</div></div>
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Queued</div><div class="font-semibold">{{ store.daemon.queued }}</div></div>
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Claimed</div><div class="font-semibold">{{ store.daemon.claimed_count }}</div></div>
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Completed</div><div class="font-semibold">{{ store.daemon.completed_count || 0 }}</div></div>
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Slots</div><div class="font-semibold">{{ store.daemon.slots ? store.daemon.slots.available : 0 }} / {{ store.daemon.slots ? store.daemon.slots.max_slots : 0 }}</div></div>
        <div class="bg-gray-50 rounded-lg p-3"><div class="text-xs text-gray-500 uppercase">Poll</div><div class="font-semibold">{{ store.daemon.poll_interval != null ? store.daemon.poll_interval + 's' : '-' }}</div></div>
      </div>
      <div v-scope="DaemonControls()"></div>
    </section>

    <section class="bg-white border rounded-lg p-4">
      <h2 class="text-lg font-semibold mb-3">Runtimes</h2>
      <div v-for="r in store.runtimes" :key="r.id" class="font-mono text-sm mb-2">{{ r.name }} ({{ r.id }}) — CLIs: {{ (r.clis || []).join(', ') }}</div>
      <div class="mt-4 border-t pt-3">
        <div class="text-xs text-gray-500 uppercase mb-2">Runtime slots</div>
        <div v-if="store.daemon.slots && store.daemon.slots.configured" class="space-y-2">
          <div class="text-sm font-mono">runtime={{ store.daemon.slots.runtime_id }} · max={{ store.daemon.slots.max_slots }} · available={{ store.daemon.slots.available }}</div>
          <div v-if="store.daemon.slots.occupied && store.daemon.slots.occupied.length" class="grid gap-2">
            <div v-for="s in store.daemon.slots.occupied" :key="s.slot_id" class="text-xs font-mono bg-amber-50 border border-amber-200 rounded-md p-2">
              slot {{ s.slot_id }} → task {{ s.task_id || '(reserved)' }}
            </div>
          </div>
          <div v-else class="text-sm text-gray-500">no occupied slots</div>
        </div>
        <div v-else class="text-sm text-gray-500">slot pool not configured</div>
      </div>
    </section>

    <section class="bg-white border rounded-lg p-4">
      <h2 class="text-lg font-semibold mb-3">Workspaces</h2>
      <table class="min-w-full text-sm">
        <thead class="bg-gray-100 text-gray-600 font-medium"><tr><th class="px-3 py-2 text-left">ID</th><th class="px-3 py-2 text-left">Slug</th><th class="px-3 py-2 text-left">Members</th><th class="px-3 py-2 text-left">Issues</th></tr></thead>
        <tbody>
          <tr v-for="w in store.workspaces" :key="w.id" class="border-b"><td class="px-3 py-2">{{ w.id }}</td><td class="px-3 py-2">{{ w.slug }}</td><td class="px-3 py-2">{{ (w.members || []).join(', ') }}</td><td class="px-3 py-2">{{ w.issue_count }}</td></tr>
        </tbody>
      </table>
      <form @submit="addMember" class="mt-3 flex gap-2">
        <input name="member_id" placeholder="member id" class="border rounded-md px-3 py-2 text-sm" required>
        <button class="btn-secondary text-sm" :disabled="store.isLoading('member.add')">+ Add to ws-1</button>
      </form>
    </section>
  </div>
`;

export function SystemView() {
  return {
    store,
    DaemonControls,
    $template: SystemViewTemplate,
    async load() {
      await loadDaemonStatus();
      store.runtimes = await api('/api/runtimes');
      store.workspaces = await api('/api/workspaces');
    },
    async addMember(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      await apiPost(`/api/workspaces/ws-1/members`, { member_id: f.get('member_id') }, 'member.add');
      e.target.reset();
      await this.load();
    },
    mounted() { this.load(); },
  };
}
