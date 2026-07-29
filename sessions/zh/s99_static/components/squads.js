import { store, api, apiPost, toast } from '../app.js';
import { EmptyState, shortId } from './common.js';

const SquadsViewTemplate = `
  <div @vue:mounted="mounted" class="h-full overflow-auto">
    <h2 class="text-lg font-semibold mb-3">Squads</h2>
    <form @submit="create" class="bg-white border rounded-lg p-4 mb-4 flex flex-wrap gap-2">
      <input name="name" placeholder="squad name" class="border rounded-md px-3 py-2 text-sm" required>
      <select name="leader_agent_id" class="border rounded-md px-3 py-2 text-sm">
        <option v-for="a in store.agents" :key="a.id" :value="a.id">{{ a.name }} ({{ a.id }})</option>
      </select>
      <input name="member_agent_ids" placeholder="agent-2,agent-3" class="border rounded-md px-3 py-2 text-sm w-48">
      <button class="btn-primary text-sm" :disabled="store.isLoading('squad.create')">+ Create</button>
    </form>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div v-for="s in store.squads" :key="s.id" class="bg-white border rounded-lg p-4">
        <h4 class="font-semibold">{{ s.name }} <span class="text-xs font-mono text-gray-500">({{ shortId(s.id) }})</span></h4>
        <p class="text-xs text-gray-500 mt-1">leader: {{ s.leader_agent_id }}</p>
        <div class="mt-2 flex flex-wrap gap-1">
          <span v-for="m in s.members" :key="m.member_id" class="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-700">
            @{{ m.member_id }} {{ m.role ? '('+m.role+')' : '' }}
          </span>
        </div>
      </div>
    </div>
    <div v-if="!store.squads.length" v-scope="EmptyState('no squads')"></div>
  </div>
`;

export function SquadsView() {
  return {
    store,
    EmptyState,
    shortId,
    $template: SquadsViewTemplate,
    async load() {
      [store.squads, store.agents] = await Promise.all([api('/api/squads'), api('/api/agents')]);
    },
    async create(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      await apiPost('/api/squads', {
        name: f.get('name'),
        leader_agent_id: f.get('leader_agent_id'),
        member_agent_ids: f.get('member_agent_ids'),
      }, 'squad.create');
      e.target.reset();
      await this.load();
      toast('squad created', 'success');
    },
    mounted() { this.load(); },
  };
}
