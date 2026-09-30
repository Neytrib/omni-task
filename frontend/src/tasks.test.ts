import { describe, expect, it, vi } from 'vitest';
import {
  TaskAPIError, isSessionError, loadTaskSnapshot, mergeTask, reduceTaskEvent, taskRequest,
} from './tasks';
import type { Task, TaskSnapshot } from './tasks';

const session = { csrf_token: 'test-csrf', expires_at: '2099-01-01T00:00:00Z' };
const task = (id: string, version = 1): Task => ({
  id, owner_id: 'owner-a', title: `Task ${id}`, content: ' Complete\ncontent 🪴 ', status: 'pending',
  source: 'dashboard', created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:00Z', version,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const page = (items: Task[], revision: number, next_cursor: string | null = null) => json({ items, revision, next_cursor });

describe('complete task snapshots', () => {
  it('collects every page before returning and preserves complete content', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(page([task('c')], 4, 'next+page'))
      .mockResolvedValueOnce(page([task('b'), task('a')], 4));
    vi.stubGlobal('fetch', fetcher);
    const snapshot = await loadTaskSnapshot(session);
    expect(snapshot.tasks.map(value => value.id)).toEqual(['c', 'b', 'a']);
    expect(snapshot.tasks[0].content).toBe(' Complete\ncontent 🪴 ');
    expect(snapshot.revision).toBe(4);
    expect(fetcher.mock.calls[1][0]).toBe('/api/tasks?limit=100&cursor=next%2Bpage');
  });

  it('discards all accumulated pages and restarts after a stale cursor', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(page([task('old')], 2, 'cursor'))
      .mockResolvedValueOnce(json({ error: { code: 'stale_cursor' } }, 409))
      .mockResolvedValueOnce(page([task('new')], 3));
    vi.stubGlobal('fetch', fetcher);
    expect((await loadTaskSnapshot(session)).tasks.map(value => value.id)).toEqual(['new']);
    expect(fetcher.mock.calls[2][0]).toBe('/api/tasks?limit=100');
  });

  it('also restarts when page revisions disagree instead of publishing a mixed snapshot', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(page([task('old')], 1, 'cursor'))
      .mockResolvedValueOnce(page([task('mixed')], 2)).mockResolvedValueOnce(page([task('new')], 3)));
    const snapshot = await loadTaskSnapshot(session);
    expect(snapshot.tasks.map(value => value.id)).toEqual(['new']);
    expect(snapshot.revision).toBe(3);
  });

  it('bounds restart attempts and does not return a partial board', async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json({ error: { code: 'stale_cursor' } }, 409)));
    vi.stubGlobal('fetch', fetcher);
    await expect(loadTaskSnapshot(session)).rejects.toMatchObject({ code: 'snapshot_busy' });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('rejects cyclic cursors instead of looping indefinitely', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(page([task('a')], 1, 'same'))));
    await expect(loadTaskSnapshot(session)).rejects.toMatchObject({ code: 'invalid_snapshot' });
  });

  it('throws on a failed later page, exposing none of its incomplete data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(page([task('a')], 1, 'next'))
      .mockRejectedValueOnce(new TypeError('offline')));
    await expect(loadTaskSnapshot(session)).rejects.toThrow('offline');
  });
});

describe('authenticated task requests', () => {
  it('uses session cookies, CSRF, version, and caller idempotency headers without changing content', async () => {
    const fetcher = vi.fn().mockResolvedValue(json(task('a'), 201));
    vi.stubGlobal('fetch', fetcher);
    const body = JSON.stringify({ content: '  <script>literal</script>\nlast line ' });
    await taskRequest('/api/tasks', session, { method: 'POST', headers: { 'Idempotency-Key': 'uuid' }, body });
    const options = fetcher.mock.calls[0][1] as RequestInit;
    expect(options.credentials).toBe('same-origin');
    expect(options.cache).toBe('no-store');
    expect(options.body).toBe(body);
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('test-csrf');
    expect(new Headers(options.headers).get('Idempotency-Key')).toBe('uuid');
  });

  it('does not put CSRF credentials in read headers or URLs', async () => {
    const fetcher = vi.fn().mockResolvedValue(json(task('a')));
    vi.stubGlobal('fetch', fetcher);
    await taskRequest('/api/tasks/a', session);
    expect(new Headers(fetcher.mock.calls[0][1].headers).has('X-CSRF-Token')).toBe(false);
    expect(fetcher.mock.calls[0][0]).toBe('/api/tasks/a');
  });

  it('maps API errors to fixed safe text instead of rendering reflected response messages', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: { code: 'source_conflict', message: '<secret>' } }, 409)));
    await expect(taskRequest('/api/tasks', session)).rejects.toThrow(
      'This submission was already used for different content. Check the board first, then choose Discard draft to start a new task.',
    );
  });

  it('recognizes expiry and invalid CSRF but does not mislabel a generic authorization failure', () => {
    expect(isSessionError(new TaskAPIError(401, 'unauthenticated', 'expired'))).toBe(true);
    expect(isSessionError(new TaskAPIError(403, 'csrf_failed', 'expired'))).toBe(true);
    expect(isSessionError(new TaskAPIError(403, 'origin_forbidden', 'bad origin'))).toBe(false);
  });
});

describe('future event handlers', () => {
  const state = (): TaskSnapshot => ({ tasks: [task('a', 2)], revision: 4, needsResync: false });

  it('handles created, updated and deleted events in revision order', () => {
    let value = reduceTaskEvent(state(), { type: 'task.created', task: task('b'), revision: 5 });
    expect(value.tasks.map(item => item.id)).toEqual(['b', 'a']);
    value = reduceTaskEvent(value, { type: 'task.updated', task: { ...task('a', 3), status: 'completed' }, revision: 6 });
    expect(value.tasks.find(item => item.id === 'a')?.status).toBe('completed');
    value = reduceTaskEvent(value, { type: 'task.deleted', task_id: 'b', revision: 7 });
    expect(value.tasks.map(item => item.id)).toEqual(['a']);
    expect(value.revision).toBe(7);
  });

  it('ignores duplicate/older revisions and refuses to resurrect a deleted task', () => {
    const deleted = reduceTaskEvent(state(), { type: 'task.deleted', task_id: 'a', revision: 5 });
    expect(reduceTaskEvent(deleted, { type: 'task.created', task: task('a'), revision: 4 })).toBe(deleted);
    expect(reduceTaskEvent(deleted, { type: 'task.updated', task: task('a', 2), revision: 5 })).toBe(deleted);
  });

  it('requires resync on revision gaps and waits for a full snapshot before applying further events', () => {
    const before = state();
    const gap = reduceTaskEvent(before, { type: 'task.deleted', task_id: 'a', revision: 6 });
    expect(gap.tasks).toBe(before.tasks);
    expect(gap.needsResync).toBe(true);
    expect(reduceTaskEvent(gap, { type: 'task.deleted', task_id: 'a', revision: 5 }).tasks).toBe(before.tasks);
  });

  it('does not replace newer task versions with delayed event or detail data', () => {
    const before = state();
    expect(mergeTask(before.tasks, task('a', 1))).toBe(before.tasks);
    const value = reduceTaskEvent(before, { type: 'task.updated', task: task('a', 1), revision: 5 });
    expect(value.tasks).toBe(before.tasks);
    expect(value.revision).toBe(5);
  });
});
