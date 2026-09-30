import type { Session } from './auth';

export const statuses = ['pending', 'in_progress', 'completed'] as const;
export type TaskStatus = typeof statuses[number];
export const statusLabels: Record<TaskStatus, string> = {
  pending: 'Pending', in_progress: 'In Progress', completed: 'Completed',
};
export type Task = {
  id: string;
  owner_id: string;
  title: string;
  content: string;
  status: TaskStatus;
  source: 'telegram_text' | 'telegram_voice' | 'dashboard';
  created_at: string;
  updated_at: string;
  version: number;
};
export type TaskSnapshot = { tasks: Task[]; revision: number; needsResync: boolean };
export type TaskEvent =
  | { type: 'task.created' | 'task.updated'; task: Task; revision: number }
  | { type: 'task.deleted'; task_id: string; revision: number };
type TaskPage = { items: Task[]; next_cursor: string | null; revision: number };

export class TaskAPIError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
    this.name = 'TaskAPIError';
  }
}

function errorMessage(status: number, code: string): string {
  if (status === 401 || (status === 403 && code === 'csrf_failed')) {
    return 'Your session has expired. Open a fresh link using /profile in Telegram.';
  }
  if (status === 403) return 'This action could not be authorized. Refresh and try again.';
  if (status === 404) return 'This task has already been deleted or is no longer available.';
  if (status === 409 && ['idempotency_conflict', 'source_conflict'].includes(code)) {
    return 'This submission was already used for different content. Check the board first, then choose Discard draft to start a new task.';
  }
  if (status === 409) return 'This task changed elsewhere. The latest version has been requested; try again.';
  if (status === 422) return 'The task could not be saved. Check the content and try again.';
  if (status === 429) return 'Too many requests. Wait a moment, then try again.';
  return 'The service could not complete this request. Please try again.';
}

export function isSessionError(error: unknown): boolean {
  return error instanceof TaskAPIError &&
    (error.status === 401 || (error.status === 403 && error.code === 'csrf_failed'));
}

export function describeTaskError(error: unknown): string {
  return error instanceof TaskAPIError ? error.message :
    'Connection lost. Your last loaded tasks are still here. Reconnect and try again.';
}

export async function taskRequest<T>(
  path: string, session: Session, options: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timeout = window.setTimeout(abort, 15_000);
  const headers = new Headers(options.headers);
  if (options.method && options.method !== 'GET') {
    headers.set('X-CSRF-Token', session.csrf_token);
    if (options.body) headers.set('Content-Type', 'application/json');
  }
  try {
    const response = await fetch(path, {
      ...options, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { error?: { code?: string } } | null;
      const code = body?.error?.code ?? 'request_failed';
      throw new TaskAPIError(response.status, code, errorMessage(response.status, code));
    }
    return response.status === 204 ? undefined as T : await response.json() as T;
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abort);
  }
}

export function sortTasks(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

export function mergeTask(tasks: Task[], task: Task): Task[] {
  const previous = tasks.find(item => item.id === task.id);
  if (previous && previous.version > task.version) return tasks;
  return sortTasks([...tasks.filter(item => item.id !== task.id), task]);
}

// Cursor revisions are owner-scoped. Publish only one complete, consistent snapshot.
export async function loadTaskSnapshot(session: Session, signal?: AbortSignal): Promise<TaskSnapshot> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const tasks = new Map<string, Task>();
    const cursors = new Set<string>();
    let cursor: string | null = null;
    let revision: number | undefined;
    try {
      do {
        const params = new URLSearchParams({ limit: '100' });
        if (cursor) params.set('cursor', cursor);
        const page = await taskRequest<TaskPage>(`/api/tasks?${params}`, session, { signal });
        if (!Array.isArray(page.items) || !Number.isSafeInteger(page.revision) || page.revision < 0) {
          throw new TaskAPIError(502, 'invalid_snapshot', 'The task list could not be read. Please refresh.');
        }
        if (revision !== undefined && revision !== page.revision) {
          throw new TaskAPIError(409, 'stale_cursor', 'The task list changed while loading. Please refresh.');
        }
        revision = page.revision;
        page.items.forEach(task => tasks.set(task.id, task));
        cursor = page.next_cursor;
        if (cursor && cursors.has(cursor)) {
          throw new TaskAPIError(502, 'invalid_snapshot', 'The task list could not be read. Please refresh.');
        }
        if (cursor) cursors.add(cursor);
      } while (cursor);
      return { tasks: sortTasks([...tasks.values()]), revision: revision ?? 0, needsResync: false };
    } catch (error) {
      if (!(error instanceof TaskAPIError && error.status === 409 && error.code === 'stale_cursor')) throw error;
      if (attempt === 2) {
        throw new TaskAPIError(409, 'snapshot_busy', 'Tasks are changing while the board loads. Please refresh in a moment.');
      }
    }
  }
  throw new Error('Snapshot retry limit reached');
}

// S7 supplies the transport. Gaps must trigger a PostgreSQL snapshot, never an
// assumed complete board assembled from lossy/out-of-order events.
export function reduceTaskEvent(state: TaskSnapshot, event: TaskEvent): TaskSnapshot {
  if (!Number.isSafeInteger(event.revision) || event.revision < 1) return { ...state, needsResync: true };
  if (event.revision <= state.revision) return state;
  if (state.needsResync || event.revision !== state.revision + 1) return { ...state, needsResync: true };
  const tasks = event.type === 'task.deleted'
    ? state.tasks.filter(task => task.id !== event.task_id)
    : mergeTask(state.tasks, event.task);
  return { tasks, revision: event.revision, needsResync: false };
}
