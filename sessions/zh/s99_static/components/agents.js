import { store, api, apiPost, apiPatch, apiDelete, toast } from '../app.js';
import { EmptyState } from './common.js';

const AgentFormTemplate = `
  <form @submit="create" class="bg-white border rounded-lg p-4 mb-4">
    <h3 class="text-sm font-semibold mb-3">Create Agent</h3>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
      <input name="id" placeholder="agent id" class="border rounded-md px-3 py-2 text-sm" required>
      <input name="name" placeholder="name" class="border rounded-md px-3 py-2 text-sm" required>
      <select name="cli" class="border rounded-md px-3 py-2 text-sm">
        <option value="claude">claude</option>
        <option value="codex">codex</option>
        <option value="copilot">copilot</option>
      </select>
    </div>
    <select name="skill_names" multiple class="border rounded-md px-3 py-2 text-sm w-full mt-3 h-24">
      <option v-for="s in store.skills" :key="s.name" :value="s.name">{{ s.name }}</option>
    </select>
    <textarea name="system_prompt" placeholder="system prompt..." class="border rounded-md px-3 py-2 text-sm w-full mt-3" rows="2"></textarea>
    <button class="btn-primary text-sm mt-3" :disabled="store.isLoading('agent.create')">+ Create</button>
  </form>
`;

export function AgentForm() {
  return {
    store,
    $template: AgentFormTemplate,
    async create(e) {
      e.preventDefault();
      const form = e.target;
      const f = new FormData(form);
      const skillNames = Array.from(form.querySelectorAll('select[name="skill_names"] option:checked')).map(o => o.value);
      await apiPost('/api/agents', {
        id: f.get('id'),
        name: f.get('name'),
        cli: f.get('cli'),
        system_prompt: f.get('system_prompt') || '',
        skill_names: skillNames.join(','),
      }, 'agent.create');
      e.target.reset();
      await this.load();
      toast('agent created', 'success');
    },
    async load() {
      store.agents = await api('/api/agents');
      store.skills = await api('/api/skills');
    },
  };
}

const AgentCardTemplate = `
  <div class="agent-card bg-white border rounded-lg p-4">
    <div class="flex items-center justify-between">
      <h4 class="font-semibold">{{ agent.name }} <span class="text-xs font-mono text-gray-500">({{ agent.id }} · {{ agent.cli }})</span></h4>
    </div>
    <div class="mt-2 flex flex-wrap gap-1">
      <span v-for="sn in agent.skill_names" :key="sn" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 text-xs">
        {{ sn }}
        <button @click="unbindSkill(sn)" class="text-indigo-400 hover:text-red-500">✕</button>
      </span>
      <span v-if="!agent.skill_names.length" class="text-xs text-gray-400">no skills</span>
    </div>
    <div class="mt-2 flex gap-2">
      <select @change="bindSkill($event.target.value); $event.target.value = ''" class="border rounded-md px-2 py-1 text-xs">
        <option value="">+ bind skill</option>
        <option v-for="s in unboundSkills" :key="s.name" :value="s.name">{{ s.name }}</option>
      </select>
    </div>
    <textarea v-model="systemPrompt" class="w-full border rounded-md px-3 py-2 text-xs mt-3 font-mono" rows="2"></textarea>
    <button @click="savePrompt" class="btn-secondary text-xs mt-2" :disabled="store.isLoading('agent.prompt.' + agent.id)">Save prompt</button>
  </div>
`;

export function AgentCard(agent) {
  return {
    agent,
    store,
    systemPrompt: agent.system_prompt || '',
    $template: AgentCardTemplate,
    async bindSkill(name) {
      if (!name) return;
      await apiPost(`/api/agents/${agent.id}/skills`, { skill_name: name }, `agent.skill`);
      await this.load();
    },
    async unbindSkill(name) {
      await apiDelete(`/api/agents/${agent.id}/skills/${encodeURIComponent(name)}`, `agent.skill`);
      await this.load();
    },
    async savePrompt() {
      await apiPatch(`/api/agents/${agent.id}`, { system_prompt: this.systemPrompt }, `agent.prompt.${agent.id}`);
      await this.load();
      toast('prompt saved', 'success');
    },
    async load() {
      store.agents = await api('/api/agents');
      store.skills = await api('/api/skills');
    },
    get unboundSkills() {
      return store.skills.filter(s => !(this.agent.skill_names || []).includes(s.name));
    },
  };
}

const AgentsViewTemplate = `
  <div @vue:mounted="mounted" class="h-full overflow-auto">
    <h2 class="text-lg font-semibold mb-3">Agents</h2>
    <div v-scope="AgentForm()"></div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div v-for="a in store.agents" :key="a.id" v-scope="AgentCard(a)"></div>
    </div>
    <div v-if="!store.agents.length" v-scope="EmptyState('no agents')"></div>
  </div>
`;

export function AgentsView() {
  return {
    store,
    AgentForm,
    AgentCard,
    EmptyState,
    $template: AgentsViewTemplate,
    async load() {
      [store.agents, store.skills] = await Promise.all([api('/api/agents'), api('/api/skills')]);
    },
    mounted() { this.load(); },
  };
}
