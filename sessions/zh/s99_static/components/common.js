import { store } from '../app.js';

const BadgeTemplate = `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium" :class="cls">{{ status }}</span>`;

export function Badge(status) {
  const colors = {
    backlog: 'bg-amber-100 text-amber-700',
    todo: 'bg-amber-100 text-amber-700',
    in_progress: 'bg-blue-100 text-blue-700',
    in_review: 'bg-blue-100 text-blue-700',
    done: 'bg-emerald-100 text-emerald-700',
    completed: 'bg-emerald-100 text-emerald-700',
    blocked: 'bg-red-100 text-red-700',
    failed: 'bg-red-100 text-red-700',
    cancelled: 'bg-gray-200 text-gray-600',
    queued: 'bg-amber-100 text-amber-700',
    dispatched: 'bg-blue-100 text-blue-700',
    running: 'bg-blue-100 text-blue-700',
  };
  return {
    status,
    cls: colors[status] || 'bg-gray-100 text-gray-600',
    $template: BadgeTemplate,
  };
}

const ToastContainerTemplate = `
  <div class="fixed top-4 right-4 z-50 space-y-2">
    <div v-for="t in store.toasts" :key="t.id"
         class="toast-item px-4 py-2 rounded-lg shadow-lg text-sm font-medium text-white"
         :class="{
           'bg-emerald-500': t.type === 'success',
           'bg-red-500': t.type === 'error',
           'bg-amber-500': t.type === 'warning',
           'bg-blue-500': t.type === 'info'
         }">
      {{ t.message }}
    </div>
  </div>
`;

export function ToastContainer() {
  return { store, $template: ToastContainerTemplate };
}

const EmptyStateTemplate = `<p class="text-gray-400 italic text-center py-8">{{ message }}</p>`;

export function EmptyState(message = 'No data') {
  return { message, $template: EmptyStateTemplate };
}

const SpinnerTemplate = `<svg class="animate-spin h-4 w-4 text-current" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>`;

export function Spinner() {
  return { $template: SpinnerTemplate };
}

export function shortId(id) {
  return id ? id.slice(0, 8) : '-';
}

export function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
