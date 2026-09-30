import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useTasks } from './useTasks';
import { App } from './App';
import { TestWebSocket } from './testWebSocket';
import type { Task } from './tasks';

const session = { csrf_token: 'synthetic-session', expires_at: '2099-01-01T00:00:00Z' };
const id = '00000000-0000-0000-0000-000000000001';
const task: Task = { id, owner_id: 'synthetic-owner', title: 'A live task', content: 'Complete original content', status: 'pending', source: 'telegram_text', version: 1, created_at: '2026-09-30T00:00:00Z', updated_at: '2026-09-30T00:00:00Z' };
const json = (value: unknown) => new Response(JSON.stringify(value));
const page = (items: Task[], revision: number) => json({ items, revision, next_cursor: null });
const socket = () => TestWebSocket.instances.at(-1)!;
const message = (value: unknown) => act(() => socket().receive(value));
const hint = (revision: number, type = 'task_updated') => message({ type, revision, task_id: id });
function server(read: () => Promise<Response> | Response) {
  const reads = vi.fn(read);
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(url === '/api/session' ? json(session) : url === `/api/tasks/${id}` ? json(task) : reads())));
  return reads;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('live owner snapshots', () => {
  it('subscribes before fetching and catches a hint received during the initial snapshot', async () => {
    TestWebSocket.autoReady = false;
    const old = deferred<Response>();
    const reads = server(vi.fn().mockReturnValueOnce(old.promise).mockImplementation(() => page([{ ...task, version: 2, status: 'completed' }], 2)));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    expect(reads).not.toHaveBeenCalled();
    expect(socket().url).toBe('ws://localhost:3000/api/ws/tasks');
    message({ type: 'ready', revision: 1, live: true });
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));
    hint(2);
    expect(reads).toHaveBeenCalledTimes(1);
    await act(async () => { old.resolve(page([task], 1)); });
    await waitFor(() => expect(result.current.revision).toBe(2));
    expect(result.current.tasks[0].status).toBe('completed');
    expect(result.current.connection).toBe('live');
    expect(reads).toHaveBeenCalledTimes(2);
  });

  it('coalesces repeated and stale hints without duplicating or regressing cards', async () => {
    const reads = server(() => page([task], 4));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    hint(4, 'task_created'); hint(3); hint(1, 'task_deleted'); hint(4);
    expect(reads).toHaveBeenCalledTimes(1);
    expect(result.current.tasks).toEqual([task]);
  });

  it('repairs a dropped final deletion using the PostgreSQL heartbeat revision', async () => {
    let deleted = false;
    server(() => page(deleted ? [] : [task], deleted ? 2 : 1));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    deleted = true;
    message({ type: 'heartbeat', revision: 2, live: true });
    await waitFor(() => expect(result.current.tasks).toEqual([]));
    expect(result.current.revision).toBe(2);
    expect(result.current.connection).toBe('live');
  });

  it('reloads both independent tab states from content-free task hints', async () => {
    let stored = task;
    server(() => page([stored], stored.version));
    const first = renderHook(() => useTasks(session, vi.fn()));
    const second = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(first.result.current.connection).toBe('live'));
    await waitFor(() => expect(second.result.current.connection).toBe('live'));
    stored = { ...task, version: 2, status: 'in_progress' };
    act(() => TestWebSocket.instances.forEach(connection => connection.receive({ type: 'task_updated', revision: 2, task_id: id })));
    await waitFor(() => expect(first.result.current.tasks[0].status).toBe('in_progress'));
    await waitFor(() => expect(second.result.current.tasks[0].status).toBe('in_progress'));
  });

  it('shows a degraded connection during subscriber loss and resnapshots after recovery', async () => {
    let revision = 1;
    const reads = server(() => page([task], revision));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    message({ type: 'resync', revision, live: false });
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.connection).toBe('reconnecting');
    revision = 2;
    message({ type: 'resync', revision, live: true });
    await waitFor(() => expect(result.current.connection).toBe('live'));
    expect(result.current.revision).toBe(2);
    expect(reads).toHaveBeenCalledTimes(3);
  });

  it('rejects an older in-flight snapshot when the socket re-registers', async () => {
    const old = deferred<Response>();
    const reads = server(vi.fn().mockReturnValueOnce(page([task], 1)).mockReturnValueOnce(old.promise)
      .mockImplementation(() => page([{ ...task, version: 3, status: 'completed' }], 3)));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    hint(2);
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(2));
    message({ type: 'resync', revision: 3, live: true });
    await act(async () => old.resolve(page([{ ...task, version: 2 }], 2)));
    await waitFor(() => expect(result.current.revision).toBe(3));
    expect(result.current.tasks[0].status).toBe('completed');
  });

  it('retries failed snapshot requests on a heartbeat even if the final hint is lost', async () => {
    const reads = server(vi.fn().mockReturnValueOnce(page([task], 1)).mockRejectedValueOnce(new TypeError('network'))
      .mockImplementation(() => page([{ ...task, version: 2 }], 2)));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    hint(2);
    await waitFor(() => expect(result.current.connection).toBe('error'));
    message({ type: 'heartbeat', revision: 2, live: true });
    await waitFor(() => expect(result.current.revision).toBe(2));
    expect(result.current.connection).toBe('live');
    expect(reads).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBeNull();
  });

  it('clears private state and closes transport on session revocation', async () => {
    const reads = server(() => page([task], 1));
    const expired = vi.fn();
    const { result } = renderHook(() => useTasks(session, expired));
    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    act(() => socket().disconnect(4401));
    expect(result.current.tasks).toEqual([]);
    expect(expired).toHaveBeenCalledOnce();
    act(() => window.dispatchEvent(new Event('focus')));
    expect(reads).toHaveBeenCalledTimes(1);
    await act(async () => expect(await result.current.changeStatus(id, 'completed')).toBe(false));
  });

  it('closes the details dialog gracefully when an authoritative snapshot removes its task', async () => {
    let deleted = false;
    server(() => page(deleted ? [] : [task], deleted ? 2 : 1));
    render(<App authentication={Promise.resolve({ state: 'signed-in', session })} />);
    fireEvent.click(await screen.findByRole('button', { name: task.title }));
    const dialog = await screen.findByRole('dialog', { name: task.title });
    await waitFor(() => expect(within(dialog).getByRole('region', { name: 'Complete task content' })).toHaveTextContent(task.content));
    deleted = true;
    hint(2, 'task_deleted');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText('A task was deleted in another window or Telegram.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'New task' })).toHaveFocus());
  });

  it('recovers creations, changes, and removals made while a tab was offline', async () => {
    const online = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
    let stored = [task, { ...task, id: '00000000-0000-0000-0000-000000000002' }];
    let revision = 2;
    server(() => page(stored, revision));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.tasks).toHaveLength(2));
    const oldSocket = socket();
    online.mockReturnValue(false);
    act(() => window.dispatchEvent(new Event('offline')));
    expect(oldSocket.readyState).toBe(3);
    expect(result.current.connection).toBe('offline');
    stored = [{ ...task, version: 2, status: 'completed' }, { ...task, id: '00000000-0000-0000-0000-000000000003' }];
    revision = 5;
    online.mockReturnValue(true);
    act(() => window.dispatchEvent(new Event('online')));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    expect(result.current.revision).toBe(5);
    expect(new Set(result.current.tasks.map(item => item.id))).toEqual(new Set(stored.map(item => item.id)));
    expect(result.current.tasks.find(item => item.id === id)?.status).toBe('completed');
    expect(TestWebSocket.instances).toHaveLength(2);
  });

  it('defers live refresh during an optimistic write, then applies the newest snapshot', async () => {
    const mutation = deferred<Response>();
    let stored = task;
    let revision = 1;
    const reads = server(() => page([stored], revision));
    const fetcher = vi.mocked(fetch);
    const normal = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url, options) => options?.method === 'PATCH' ? mutation.promise : normal(url, options));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.connection).toBe('live'));
    let write!: Promise<boolean>;
    act(() => { write = result.current.changeStatus(id, 'completed'); });
    hint(2); hint(3); hint(3);
    expect(reads).toHaveBeenCalledTimes(1);
    expect(result.current.connection).toBe('reconnecting');
    stored = { ...task, status: 'in_progress', version: 3 };
    revision = 3;
    await act(async () => { mutation.resolve(json({ ...task, status: 'completed', version: 2 })); await write; });
    await waitFor(() => expect(result.current.connection).toBe('live'));
    expect(result.current.tasks[0]).toEqual(stored);
    expect(reads).toHaveBeenCalledTimes(2);
  });

  it('closes the socket when logout unmounts the private board', async () => {
    server(() => page([task], 1));
    render(<App authentication={Promise.resolve({ state: 'signed-in', session })} />);
    await screen.findByRole('button', { name: task.title });
    const connection = socket();
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    await screen.findByText('Open your private board.');
    expect(connection.readyState).toBe(3);
    expect(connection.onmessage).toBeNull();
  });
});
