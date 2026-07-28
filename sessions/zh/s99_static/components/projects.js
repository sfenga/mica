import { store, api, apiPost, toast } from '../app.js';
import { EmptyState } from './common.js';

const DirectoryPickerTemplate = `
  <div v-if="store.showDirPicker" @vue:mounted="load" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
    <div class="bg-white rounded-xl shadow-2xl w-[600px] max-h-[80vh] flex flex-col">
      <div class="flex items-center justify-between p-4 border-b">
        <h3 class="font-semibold">Select directory</h3>
        <button @click="close" class="text-gray-400 hover:text-gray-600">✕</button>
      </div>
      <div class="p-4 text-xs font-mono text-gray-500 truncate">{{ current }}</div>
      <div class="flex-1 overflow-y-auto p-2">
        <div v-if="parent !== null" @click="load(parent)" class="p-2 hover:bg-indigo-50 cursor-pointer rounded">↑ parent</div>
        <div v-for="d in dirs" :key="d.path" @click="load(d.path)" class="p-2 hover:bg-indigo-50 cursor-pointer rounded font-mono text-sm">📁 {{ d.name }}</div>
        <div v-if="!dirs.length" v-scope="EmptyState('no subdirectories')"></div>
      </div>
      <div class="p-4 border-t flex justify-end gap-2">
        <button @click="close" class="btn-secondary text-sm">Cancel</button>
        <button @click="select" class="btn-primary text-sm">Select</button>
      </div>
    </div>
  </div>
`;

export function DirectoryPicker() {
  return {
    store,
    EmptyState,
    current: '',
    parent: null,
    dirs: [],
    $template: DirectoryPickerTemplate,
    open() { store.showDirPicker = true; this.load(); },
    close() { store.showDirPicker = false; },
    async load(path = '') {
      const d = await api('/api/fs/browse' + (path ? `?path=${encodeURIComponent(path)}` : ''));
      this.current = d.current;
      this.parent = d.parent;
      this.dirs = d.dirs;
    },
    select() {
      store.projectPath = this.current;
      this.close();
    },
  };
}

const ProjectFormTemplate = `
  <form @submit="create" class="bg-white border rounded-lg p-4 mb-4">
    <h3 class="text-sm font-semibold mb-3">Create Project</h3>
    <div class="flex flex-wrap gap-2">
      <input name="name" placeholder="project name" class="border rounded-md px-3 py-2 text-sm" required>
      <div class="flex gap-2">
        <input name="local_path" :value="store.projectPath" placeholder="/path/to/repo" class="border rounded-md px-3 py-2 text-sm w-64">
        <button type="button" @click="openPicker" class="btn-secondary text-sm">📁</button>
      </div>
      <input name="repo_url" placeholder="git url (optional)" class="border rounded-md px-3 py-2 text-sm w-64">
      <button class="btn-primary text-sm" :disabled="store.isLoading('project.create')">+ Create</button>
    </div>
  </form>
`;

export function ProjectForm() {
  return {
    store,
    $template: ProjectFormTemplate,
    async create(e) {
      e.preventDefault();
      const f = new FormData(e.target);
      await apiPost('/api/projects', {
        name: f.get('name'),
        local_path: f.get('local_path') || '',
        repo_url: f.get('repo_url') || '',
      }, 'project.create');
      e.target.reset();
      store.projectPath = '';
      await this.load();
      toast('project created', 'success');
    },
    openPicker() { store.showDirPicker = true; },
    async load() { store.projects = await api('/api/projects'); },
  };
}

const ProjectsViewTemplate = `
  <div @vue:mounted="mounted" class="h-full overflow-auto">
    <h2 class="text-lg font-semibold mb-3">Projects</h2>
    <div v-scope="ProjectForm()"></div>
    <div v-scope="DirectoryPicker()"></div>
    <table class="min-w-full text-sm">
      <thead class="bg-gray-100 text-gray-600 font-medium"><tr><th class="px-3 py-2 text-left">Name</th><th class="px-3 py-2 text-left">Local Path</th><th class="px-3 py-2 text-left">Repo</th></tr></thead>
      <tbody>
        <tr v-for="p in store.projects" :key="p.id" class="border-b hover:bg-gray-50">
          <td class="px-3 py-2 font-medium">{{ p.name }}</td>
          <td class="px-3 py-2 font-mono text-xs text-gray-600">{{ p.local_path || '-' }}</td>
          <td class="px-3 py-2 text-xs text-gray-500">{{ p.repo_url || '-' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="!store.projects.length" v-scope="EmptyState('no projects')"></div>
  </div>
`;

export function ProjectsView() {
  return {
    store,
    ProjectForm,
    DirectoryPicker,
    EmptyState,
    $template: ProjectsViewTemplate,
    async load() { store.projects = await api('/api/projects'); },
    mounted() { this.load(); },
  };
}
