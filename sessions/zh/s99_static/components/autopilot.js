import { store, api, apiPost, toast } from '../app.js';
import { EmptyState, shortId } from './common.js';

const AutopilotViewTemplate = `
  <div @vue:mounted="mounted" class="h-full overflow-auto">
    <h2 class="text-lg font-semibold mb-3">Autopilot</h2>
    <form @submit="create" class="bg-white border rounded-lg p-4 mb-4 flex flex-wrap gap-2">
      <input name="cron" placeholder="0 9 * * *" class="border rounded-md px-3 py-2 text-sm font-mono" required>
      <input name="agent_id" placeholder="agent-1" value="agent-1" class="border rounded-md px-3 py-2 text-sm">
      <input name="title" placeholder="title" class="border rounded-md px-3 py-2 text-sm" required>
      <button class="btn-primary text-sm" :disabled="store.isLoading('autopilot.create')">+ Create</button>
    </form>
    <table class="min-w-full text-sm">
      <thead class="bg-gray-100 text-gray-600 font-medium"><tr><th class="px-3 py-2 text-left">ID</th><th class="px-3 py-2 text-left">Cron</th><th class="px-3 py-2 text-left">Agent</th><th class="px-3 py-2 text-left">Title</th><th class="px-3 py-2 text-left">Next</th></tr></thead>
      <tbody>
        <tr v-for="a in store.autopilots" :key="a.id" class="border-b hover:bg-gray-50">
          <td class="px-3 py-2 font-mono">{{ shortId(a.id) }}</td>
          <td class="px-3 py-2 font-mono">{{ a.cron }}</td>
          <td class="px-3 py-2">{{ a.agent_id }}</td>
          <td class="px-3 py-2">{{ a.title }}</td>
          <td class="px-3 py-2 text-gray-500">{{ a.next_run ? a.next_run.slice(11,16) : '-' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="!store.autopilots.length" v-scope="EmptyState('no autopilots')"></div>
  </div>
`;

export function AutopilotView() {
  return {
    store,
    EmptyState,
    shortId,
    $template: AutopilotViewTemplate,
    async load() { store.autopilots = await api('/api/autopilots'); },
    async create(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      await apiPost('/api/autopilots', {
        cron: f.get('cron'),
        agent_id: f.get('agent_id'),
        title: f.get('title'),
      }, 'autopilot.create');
      e.target.reset();
      await this.load();
      toast('autopilot created', 'success');
    },
    mounted() { this.load(); },
  };
}
