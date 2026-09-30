import { StrictMode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useTasks } from './useTasks';
import { TestWebSocket } from './testWebSocket';
import type { Task } from './tasks';

const session = { csrf_token: 'test-csrf', expires_at: '2099-01-01T00:00:00Z' };
const task = (id = 'a', version = 1): Task => ({
  id, owner_id: 'owner-a', title: `Task ${id}`, content: ' Complete\ncontent 🪴 ', status: 'pending',
  source: 'telegram_voice', created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:00Z', version,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const page = (items: Task[], revision = 1) => json({ items, revision, next_cursor: null });
const apiError = (status: number, code = 'failed') => json({ error: { code } }, status);
function stubTaskFetch(fetcher: (url: string, options: RequestInit) => Promise<Response>,
  sessionReply: () => Response | Promise<Response> = () => json(session)) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, options: RequestInit) =>
    url === '/api/session' ? Promise.resolve(sessionReply()) : fetcher(url, options)));
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

async function boardWithStatusNotice(items = [task()]) {
  let stored = items;
  let revision = 1;
  stubTaskFetch(async (url, options) => {
    if (options.method === 'PATCH') {
      const id = url.split('/').at(-1);
      const status = JSON.parse(options.body as string).status;
      stored = stored.map(item => item.id === id ? { ...item, status, version: item.version + 1 } : item);
      revision += 1;
      return json(stored.find(item => item.id === id));
    }
    return page(stored, revision);
  });
  const hook = renderHook(() => useTasks(session, vi.fn()));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  vi.useFakeTimers();
  await act(async () => { await hook.result.current.changeStatus(items[0].id, 'completed'); });
  expect(hook.result.current.notice).toBe('Task moved to Completed.');
  return hook;
}

describe('transient task notifications', () => {
  it('dismisses successful status feedback after four seconds', async () => {
    const { result } = await boardWithStatusNotice();
    await act(async () => { await vi.advanceTimersByTimeAsync(3999); });
    expect(result.current.notice).toBe('Task moved to Completed.');
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.notice).toBe('');
    expect(result.current.tasks[0].status).toBe('completed');
  });

  it('gives a repeated identical notification its own full lifetime', async () => {
    const { result } = await boardWithStatusNotice([task('a'), task('b')]);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    await act(async () => { await result.current.changeStatus('b', 'completed'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3999); });
    expect(result.current.notice).toBe('Task moved to Completed.');
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.notice).toBe('');
  });

  it('does not extend a notification when unchanged tasks are refreshed', async () => {
    const { result } = await boardWithStatusNotice();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    await act(async () => { await result.current.refresh(); });
    expect(result.current.notice).toBe('Task moved to Completed.');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.notice).toBe('');
  });

  it('cancels the outstanding notification timer when the board unmounts', async () => {
    const { unmount } = await boardWithStatusNotice();
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('task board data state', () => {
  it('loads actual API data and becomes live after a subscribed authoritative snapshot', async () => {
    stubTaskFetch(vi.fn().mockResolvedValue(page([task()])));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    expect(result.current.tasks[0].content).toBe(' Complete\ncontent 🪴 ');
    expect(result.current.connection).toBe('live');
    expect(result.current.loading).toBe(false);
  });

  it('publishes no partial board while a later page is still loading', async () => {
    const next = deferred<Response>();
    stubTaskFetch(vi.fn().mockResolvedValueOnce(json({ items: [task('first')], revision: 1, next_cursor: 'next' }))
      .mockReturnValueOnce(next.promise));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => url !== '/api/session')).toHaveLength(2));
    expect(result.current.tasks).toEqual([]);
    expect(result.current.loading).toBe(true);
    await act(async () => { next.resolve(page([task('second')])); });
    expect(result.current.tasks).toHaveLength(2);
  });

  it('rolls back a failed optimistic status change and keeps a useful error', async () => {
    const update = deferred<Response>();
    const fetcher = vi.fn().mockImplementation((_url: string, options: RequestInit) =>
      options.method === 'PATCH' ? update.promise : Promise.resolve(page([task()])));
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let operation!: Promise<boolean>;
    act(() => { operation = result.current.changeStatus('a', 'completed'); });
    expect(result.current.tasks[0].status).toBe('completed');
    expect(result.current.pendingIds.has('a')).toBe(true);
    await act(async () => { update.resolve(apiError(503)); expect(await operation).toBe(false); });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.tasks[0].status).toBe('pending');
    expect(result.current.pendingIds.size).toBe(0);
    expect(result.current.error).toMatch(/service could not complete/i);
  });

  it('serializes repeated writes for a task and includes its original version', async () => {
    const update = deferred<Response>();
    let stored = task();
    const fetcher = vi.fn().mockImplementation((_url: string, options: RequestInit) =>
      options.method === 'PATCH' ? update.promise : Promise.resolve(page([stored], stored.version)));
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let operation!: Promise<boolean>;
    act(() => { operation = result.current.changeStatus('a', 'in_progress'); });
    await act(async () => { expect(await result.current.changeStatus('a', 'completed')).toBe(false); });
    expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PATCH')).toHaveLength(1);
    const options = fetcher.mock.calls.find(([, value]) => value.method === 'PATCH')![1];
    expect(new Headers(options.headers).get('If-Match')).toBe('1');
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('test-csrf');
    stored = { ...task('a', 2), status: 'in_progress' };
    await act(async () => { update.resolve(json(stored)); expect(await operation).toBe(true); });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.tasks[0]).toEqual(stored);
  });

  it('refreshes authoritative details after a conflicting status write', async () => {
    let listReads = 0;
    const newer = { ...task('a', 3), status: 'in_progress' as const };
    stubTaskFetch(vi.fn().mockImplementation((url: string, options: RequestInit) => {
      if (options.method === 'PATCH') return Promise.resolve(apiError(409, 'version_conflict'));
      if (url === '/api/tasks/a') return Promise.resolve(json(newer));
      return Promise.resolve(page([++listReads === 1 ? task() : newer], listReads === 1 ? 1 : 3));
    }));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { expect(await result.current.changeStatus('a', 'completed')).toBe(false); });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.tasks[0]).toEqual(newer);
    expect(result.current.error).toMatch(/changed elsewhere/);
  });

  it('preserves a newer event when an older optimistic write fails', async () => {
    const update = deferred<Response>();
    let stored = task();
    stubTaskFetch(vi.fn().mockImplementation((_url: string, options: RequestInit) =>
      options.method === 'PATCH' ? update.promise : Promise.resolve(page([stored], stored.version))));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let operation!: Promise<boolean>;
    act(() => { operation = result.current.changeStatus('a', 'completed'); });
    stored = { ...task('a', 2), status: 'in_progress' };
    act(() => { result.current.applyEvent({ type: 'task.updated', task: stored, revision: 2 }); });
    await act(async () => { update.resolve(apiError(503)); await operation; });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.tasks[0]).toEqual(stored);
  });

  it('reuses the supplied idempotency key and exact content after an uncertain create response', async () => {
    let attempts = 0;
    const content = '  <b>Keep as text</b>\nLast line.  ';
    const created = { ...task('new'), content };
    const fetcher = vi.fn().mockImplementation((_url: string, options: RequestInit) => {
      if (options.method === 'POST') return ++attempts === 1 ? Promise.reject(new TypeError('network')) : Promise.resolve(json(created, 200));
      return Promise.resolve(page(attempts === 2 ? [created] : []));
    });
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { expect(await result.current.createTask(content, 'same-key')).toBeNull(); });
    await act(async () => { expect(await result.current.createTask(content, 'same-key')).toEqual(created); });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    const posts = fetcher.mock.calls.filter(([, options]) => options.method === 'POST');
    expect(posts).toHaveLength(2);
    posts.forEach(([, options]) => {
      expect(new Headers(options.headers).get('Idempotency-Key')).toBe('same-key');
      expect(JSON.parse(options.body as string).content).toBe(content);
    });
    expect(result.current.tasks).toEqual([created]);
    expect(result.current.newTaskIds.has('new')).toBe(true);
  });

  it('deduplicates rapid create submissions using the same form key', async () => {
    const response = deferred<Response>();
    stubTaskFetch(vi.fn().mockImplementation((_url: string, options: RequestInit) =>
      options.method === 'POST' ? response.promise : Promise.resolve(page([]))));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let first!: Promise<Task | null>;
    act(() => { first = result.current.createTask('content', 'same-key'); });
    await act(async () => { expect(await result.current.createTask('content', 'same-key')).toBeNull(); });
    expect(vi.mocked(fetch).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
    await act(async () => { response.resolve(json(task(), 201)); await first; });
  });

  it('releases failed deletion locks so retry is possible and treats 404 as already deleted', async () => {
    let deletions = 0;
    const fetcher = vi.fn().mockImplementation((_url: string, options: RequestInit) => {
      if (options.method === 'DELETE') return Promise.resolve(apiError(++deletions === 1 ? 503 : 404));
      return Promise.resolve(page(deletions < 2 ? [task()] : [], deletions < 2 ? 1 : 2));
    });
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { expect(await result.current.deleteTask('a')).toBe(false); });
    expect(result.current.pendingIds.has('a')).toBe(false);
    expect(result.current.tasks).toHaveLength(1);
    await act(async () => { expect(await result.current.deleteTask('a')).toBe(true); });
    await waitFor(() => expect(result.current.tasks).toHaveLength(0));
    expect(result.current.pendingIds.size).toBe(0);
  });

  it('keeps cached tasks on disconnection, rejects offline writes and refreshes after reconnect', async () => {
    const online = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(page([task()])));
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    online.mockReturnValue(false);
    act(() => { window.dispatchEvent(new Event('offline')); });
    expect(result.current.connection).toBe('offline');
    await act(async () => { expect(await result.current.changeStatus('a', 'completed')).toBe(false); });
    expect(result.current.tasks).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
    online.mockReturnValue(true);
    act(() => { window.dispatchEvent(new Event('online')); });
    await waitFor(() => expect(result.current.connection).toBe('live'));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('retains the last full snapshot when a manual refresh fails', async () => {
    stubTaskFetch(vi.fn().mockResolvedValueOnce(page([task()])).mockRejectedValue(new TypeError('network')));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.tasks).toEqual([task()]);
    expect(result.current.connection).toBe('error');
    expect(result.current.error).toMatch(/Connection lost/);
  });

  it('does not send a request when selecting the current status again', async () => {
    const fetcher = vi.fn().mockResolvedValue(page([task()]));
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { expect(await result.current.changeStatus('a', 'pending')).toBe(true); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
  });

  it.each([401, 403])('clears private data after an expired/invalid session response (%s)', async status => {
    const expired = vi.fn();
    stubTaskFetch(vi.fn().mockResolvedValueOnce(page([task()]))
      .mockResolvedValueOnce(apiError(status, status === 403 ? 'csrf_failed' : 'session_expired')));
    const { result } = renderHook(() => useTasks(session, expired));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { await result.current.changeStatus('a', 'completed'); });
    expect(result.current.tasks).toEqual([]);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('does not let a delayed snapshot overwrite a successful optimistic mutation', async () => {
    const oldSnapshot = deferred<Response>();
    let reads = 0;
    let stored = task();
    stubTaskFetch(vi.fn().mockImplementation((_url: string, options: RequestInit) => {
      if (options.method === 'PATCH') {
        stored = { ...task('a', 2), status: 'completed' };
        return Promise.resolve(json(stored));
      }
      reads += 1;
      return reads === 2 ? oldSnapshot.promise : Promise.resolve(page([stored], stored.version));
    }));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let refresh!: Promise<void>;
    act(() => { refresh = result.current.refresh(); });
    await waitFor(() => expect(reads).toBe(2));
    await act(async () => { await result.current.changeStatus('a', 'completed'); });
    await act(async () => { oldSnapshot.resolve(page([task()])); await refresh; });
    expect(result.current.tasks[0].status).toBe('completed');
    expect(result.current.tasks[0].version).toBe(2);
  });

  it('does not resurrect a deleted task from an earlier detail request', async () => {
    const details = deferred<Response>();
    let removed = false;
    stubTaskFetch(vi.fn().mockImplementation((url: string, options: RequestInit) => {
      if (options.method === 'DELETE') { removed = true; return Promise.resolve(new Response(null, { status: 204 })); }
      if (url === '/api/tasks/a') return details.promise;
      return Promise.resolve(page(removed ? [] : [task()], removed ? 2 : 1));
    }));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let read!: Promise<Task | null>;
    act(() => { read = result.current.getTask('a'); });
    await act(async () => { await result.current.deleteTask('a'); });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    await act(async () => { details.resolve(json(task())); expect(await read).toBeNull(); });
    expect(result.current.tasks).toEqual([]);
  });

  it('resynchronizes a revision gap using the API snapshot', async () => {
    stubTaskFetch(vi.fn().mockResolvedValueOnce(page([task()], 1)).mockResolvedValueOnce(page([task('new')], 3)));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => { result.current.applyEvent({ type: 'task.created', task: task('new'), revision: 3 }); });
    expect(result.current.needsResync).toBe(true);
    await waitFor(() => expect(result.current.needsResync).toBe(false));
    expect(result.current.tasks.map(item => item.id)).toEqual(['new']);
    expect(result.current.revision).toBe(3);
  });

  it('retries a snapshot invalidated by newer details even when no mutation is pending', async () => {
    const oldSnapshot = deferred<Response>();
    let reads = 0;
    const newer = { ...task('a', 2), status: 'completed' as const };
    stubTaskFetch(vi.fn().mockImplementation((url: string) => {
      if (url === '/api/tasks/a') return Promise.resolve(json(newer));
      reads += 1;
      if (reads === 1) return Promise.resolve(page([task()]));
      if (reads === 2) return oldSnapshot.promise;
      return Promise.resolve(page([newer], 2));
    }));
    const { result } = renderHook(() => useTasks(session, vi.fn()));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let refresh!: Promise<void>;
    act(() => { refresh = result.current.refresh(); });
    await waitFor(() => expect(reads).toBe(2));
    await act(async () => { await result.current.getTask('a'); });
    await act(async () => { oldSnapshot.resolve(page([task()])); await refresh; });
    await waitFor(() => expect(result.current.connection).toBe('live'));
    expect(result.current.tasks).toEqual([newer]);
    expect(reads).toBe(3);
  });

  it('clears another session’s data and ignores its delayed responses', async () => {
    const details = deferred<Response>();
    let cookieSession = session;
    const fetcher = vi.fn().mockResolvedValueOnce(page([task()])).mockReturnValueOnce(details.promise)
      .mockResolvedValue(page([{ ...task('b'), owner_id: 'owner-b' }]));
    stubTaskFetch(fetcher, () => json(cookieSession));
    const { result, rerender } = renderHook(({ csrf }) => useTasks({ ...session, csrf_token: csrf }, vi.fn()), { initialProps: { csrf: session.csrf_token } });
    await waitFor(() => expect(result.current.loading).toBe(false));
    let read!: Promise<Task | null>;
    act(() => { read = result.current.getTask('a'); });
    cookieSession = { ...session, csrf_token: 'second-session' };
    rerender({ csrf: cookieSession.csrf_token });
    expect(result.current.tasks).toEqual([]);
    await waitFor(() => expect(result.current.tasks[0]?.owner_id).toBe('owner-b'));
    await act(async () => { details.resolve(json(task())); expect(await read).toBeNull(); });
    expect(result.current.tasks.map(value => value.id)).toEqual(['b']);
  });

  it('closes the obsolete StrictMode socket before taking the active snapshot', async () => {
    const fetcher = vi.fn().mockResolvedValue(page([task('current')], 2));
    stubTaskFetch(fetcher);
    const { result } = renderHook(() => useTasks(session, vi.fn()), { wrapper: StrictMode });
    await waitFor(() => expect(result.current.tasks[0]?.id).toBe('current'));
    expect(TestWebSocket.instances).toHaveLength(2);
    expect(TestWebSocket.instances[0].readyState).toBe(3);
    expect(TestWebSocket.instances[0].onmessage).toBeNull();
    expect(fetcher).toHaveBeenCalledOnce();
    expect(result.current.tasks.map(value => value.id)).toEqual(['current']);
  });

  it('clears the old account before reading a lower-revision board after a cookie switch in another tab', async () => {
    const expired = vi.fn();
    let cookieSession = session;
    const fetcher = vi.fn().mockResolvedValueOnce(page([task()], 40))
      .mockResolvedValue(page([{ ...task('b'), owner_id: 'owner-b' }], 1));
    stubTaskFetch(fetcher, () => json(cookieSession));
    const { result } = renderHook(() => useTasks(session, expired));
    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    cookieSession = { ...session, csrf_token: 'another-account-session' };
    act(() => { window.dispatchEvent(new Event('focus')); });
    await waitFor(() => expect(expired).toHaveBeenCalledTimes(1));
    expect(result.current.tasks).toEqual([]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(expired).toHaveBeenCalledWith(expect.stringContaining('changed in another tab'));
  });

  it('rejects a complete snapshot if the shared cookie changes while pages are loading', async () => {
    const expired = vi.fn();
    const snapshot = deferred<Response>();
    let cookieSession = session;
    const fetcher = vi.fn().mockResolvedValueOnce(page([task()], 40)).mockReturnValueOnce(snapshot.promise);
    stubTaskFetch(fetcher, () => json(cookieSession));
    const { result } = renderHook(() => useTasks(session, expired));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let refresh!: Promise<void>;
    act(() => { refresh = result.current.refresh(); });
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    cookieSession = { ...session, csrf_token: 'another-account-session' };
    await act(async () => {
      snapshot.resolve(page([{ ...task('b'), owner_id: 'owner-b' }], 1));
      await refresh;
    });
    expect(result.current.tasks).toEqual([]);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('clears cached tasks on a 401 session check before refreshing the board', async () => {
    const expired = vi.fn();
    let authenticated = true;
    const fetcher = vi.fn().mockResolvedValueOnce(page([task()]));
    stubTaskFetch(fetcher, () => authenticated ? json(session) : apiError(401, 'session_expired'));
    const { result } = renderHook(() => useTasks(session, expired));
    await waitFor(() => expect(result.current.loading).toBe(false));
    authenticated = false;
    await act(async () => { await result.current.refresh(); });
    expect(result.current.tasks).toEqual([]);
    expect(expired).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
