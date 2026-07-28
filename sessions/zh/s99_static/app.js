import { createApp } from 'https://unpkg.com/petite-vue@0.4.1/dist/petite-vue.es.js';

const store = {
  tab: 'board',
};

const tabs = [
  { key: 'board', label: 'Board' },
  { key: 'queue', label: 'Queue' },
  { key: 'agents', label: 'Agents' },
  { key: 'projects', label: 'Projects' },
  { key: 'squads', label: 'Squads' },
  { key: 'autopilot', label: 'Autopilot' },
  { key: 'system', label: 'System' },
];

function App() {
  return {
    store,
    tabs,
    get currentTabLabel() {
      const t = tabs.find(x => x.key === store.tab);
      return t ? t.label : 'Board';
    },
  };
}

createApp({ App }).mount('#app');
