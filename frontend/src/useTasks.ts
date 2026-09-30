import { useCallback, useEffect, useRef, useState } from 'react';
import type { Session } from './auth';
import { connectLiveTasks } from './liveTasks';
import {
  TaskAPIError, describeTaskError, isSessionError, loadTaskSnapshot, mergeTask,
  reduceTaskEvent, statusLabels, taskRequest,
} from './tasks';
import type { Task, TaskEvent, TaskSnapshot, TaskStatus } from './tasks';

export type ConnectionState = 'connecting' | 'ready' | 'live' | 'reconnecting' | 'offline' | 'error';
type BoardState = TaskSnapshot & {
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  connection: ConnectionState;
  pendingIds: Set<string>;
  newTaskIds: Set<string>;
  notice: string;
};
const initial = (): BoardState => ({
  tasks: [], revision: 0, needsResync: false, loading: true, refreshing: false,
  error: null, connection: navigator.onLine ? 'connecting' : 'offline',
  pendingIds: new Set(), newTaskIds: new Set(), notice: '',
});

export function useTasks(session: Session, onExpired: (message?: string) => void) {
  const [state, setState] = useState<BoardState>(initial);
  const latest = useRef(state);
  const active = useRef(false);
  const expired = useRef(false);
  const generation = useRef(0);
  const epoch = useRef(0);
  const refreshNumber = useRef(0);
  const refreshController = useRef<AbortController | null>(null);
  const controllers = useRef(new Set<AbortController>());
  const locks = useRef(new Set<string>());
  const tombstones = useRef(new Set<string>());
  const refreshAfterWrite = useRef(false);
  const requestRefresh = useRef<(preserveError?: boolean, restart?: number) => Promise<void>>(async () => {});
  const newTimers = useRef(new Set<number>());
  const noticeTimer = useRef<number | undefined>(undefined);
  const snapshotError = useRef<string | null>(null);
  const liveReady = useRef(false);
  const liveAvailable = useRef(false);
  const wantedRevision = useRef(0);
  const syncCycle = useRef(0);
  const refreshAfterFlight = useRef(false);
  const stopLive = useRef<(() => void) | undefined>(undefined);
  const expireHandler = useRef(onExpired);
  expireHandler.current = onExpired;

  const commit = useCallback((update: (value: BoardState) => BoardState) => {
    if (!active.current) return;
    latest.current = update(latest.current);
    setState(latest.current);
  }, []);
  const valid = useCallback((started: number) => active.current && !expired.current && generation.current === started, []);
  const fail = useCallback((error: unknown) => {
    if (isSessionError(error)) {
      if (expired.current) return;
      expired.current = true;
      stopLive.current?.();
      generation.current += 1;
      controllers.current.forEach(controller => controller.abort());
      refreshController.current?.abort();
      window.clearTimeout(noticeTimer.current);
      noticeTimer.current = undefined;
      commit(value => ({ ...value, tasks: [], pendingIds: new Set(), newTaskIds: new Set(), loading: false, refreshing: false, error: null, notice: '' }));
      expireHandler.current(describeTaskError(error));
      return;
    }
    commit(value => ({ ...value, error: describeTaskError(error),
      connection: navigator.onLine ? (error instanceof TaskAPIError ? value.connection : 'error') : 'offline',
    }));
  }, [commit]);

  const showNotice = useCallback((notice: string) => {
    if (!active.current || expired.current) return;
    window.clearTimeout(noticeTimer.current);
    commit(value => ({ ...value, notice }));
    // Restart even when consecutive actions produce the same message.
    noticeTimer.current = window.setTimeout(() => {
      noticeTimer.current = undefined;
      commit(value => ({ ...value, notice: '' }));
    }, 4000);
  }, [commit]);

  const highlight = useCallback((ids: string[]) => {
    if (!ids.length) return;
    commit(value => ({ ...value, newTaskIds: new Set([...value.newTaskIds, ...ids]) }));
    const timer = window.setTimeout(() => {
      newTimers.current.delete(timer);
      commit(value => ({ ...value, newTaskIds: new Set([...value.newTaskIds].filter(id => !ids.includes(id))) }));
    }, 3500);
    newTimers.current.add(timer);
  }, [commit]);

  const checkSession = useCallback(async (signal: AbortSignal) => {
    const current = await taskRequest<Session>('/api/session', session, { signal });
    if (current.csrf_token !== session.csrf_token) {
      throw new TaskAPIError(401, 'session_changed', 'Your session changed in another tab. Open a fresh /profile link in Telegram.');
    }
  }, [session.csrf_token]);

  const refresh = useCallback(async (preserveError = false, restart = 0): Promise<void> => {
    if (!active.current || expired.current) return;
    if (!navigator.onLine) {
      commit(value => ({ ...value, connection: 'offline', loading: false, refreshing: false }));
      return;
    }
    if (locks.current.size) { refreshAfterWrite.current = true; return; }
    const started = generation.current;
    const dataEpoch = epoch.current;
    const cycle = syncCycle.current;
    refreshAfterFlight.current = false;
    const request = ++refreshNumber.current;
    refreshController.current?.abort();
    const controller = new AbortController();
    refreshController.current = controller;
    commit(value => ({ ...value, refreshing: !value.loading,
      connection: value.loading ? 'connecting' : liveReady.current && liveAvailable.current && !value.needsResync && value.revision >= wantedRevision.current ? 'live' : 'reconnecting', error: preserveError ? value.error : null,
    }));
    try {
      // Tabs share the HttpOnly cookie. A new login in another tab must not mix
      // the previous account's cached board with a new account's snapshot.
      await checkSession(controller.signal);
      if (!valid(started) || request !== refreshNumber.current || controller.signal.aborted) return;
      const snapshot = await loadTaskSnapshot(session, controller.signal);
      if (!valid(started) || request !== refreshNumber.current || controller.signal.aborted) return;
      await checkSession(controller.signal);
      if (!valid(started) || request !== refreshNumber.current || controller.signal.aborted) return;
      if (dataEpoch !== epoch.current || snapshot.revision < latest.current.revision || snapshot.revision < wantedRevision.current || cycle !== syncCycle.current) {
        commit(value => ({ ...value, refreshing: false, needsResync: true }));
        if (locks.current.size) refreshAfterWrite.current = true;
        else if (restart < 2) void requestRefresh.current(true, restart + 1);
        else {
          snapshotError.current = 'Tasks are changing while the board loads. Retrying automatically.';
          commit(value => ({ ...value, loading: false, connection: 'reconnecting', error: snapshotError.current }));
        }
        return;
      }
      const previous = latest.current;
      const newIds = previous.loading ? [] : snapshot.tasks.filter(task => !previous.tasks.some(old => old.id === task.id)).map(task => task.id);
      const removed = previous.tasks.filter(task => !snapshot.tasks.some(item => item.id === task.id));
      removed.forEach(task => tombstones.current.add(task.id));
      commit(value => ({ ...value, ...snapshot, loading: false, refreshing: false,
        error: value.error === snapshotError.current ? null : value.error,
        connection: navigator.onLine ? liveReady.current && liveAvailable.current ? 'live' : 'reconnecting' : 'offline',
      }));
      snapshotError.current = null;
      if (newIds.length) showNotice(`${newIds.length} new ${newIds.length === 1 ? 'task' : 'tasks'} added.`);
      if (removed.length && !newIds.length) showNotice('A task was deleted in another window or Telegram.');
      highlight(newIds);
    } catch (error) {
      if (!valid(started) || request !== refreshNumber.current || controller.signal.aborted) return;
      snapshotError.current = describeTaskError(error);
      fail(error);
      if (!expired.current) commit(value => ({ ...value, loading: false, refreshing: false, connection: navigator.onLine ? 'error' : 'offline' }));
    } finally {
      if (request === refreshNumber.current) {
        refreshController.current = null;
        if (valid(started) && refreshAfterFlight.current) {
          refreshAfterFlight.current = false;
          void requestRefresh.current(true);
        }
      }
    }
  }, [session.csrf_token, checkSession, commit, fail, highlight, showNotice, valid]);
  requestRefresh.current = refresh;

  useEffect(() => {
    active.current = true;
    expired.current = false;
    generation.current += 1;
    latest.current = initial();
    setState(latest.current);
    locks.current.clear();
    tombstones.current.clear();
    refreshAfterWrite.current = false;
    snapshotError.current = null;
    liveReady.current = false;
    liveAvailable.current = false;
    wantedRevision.current = 0;
    syncCycle.current = 0;
    refreshAfterFlight.current = false;
    refreshController.current = null;
    const refreshWhenIdle = (force = false) => {
      if (!active.current || expired.current) return;
      if (locks.current.size) { refreshAfterWrite.current = true; return; }
      if (refreshController.current) {
        if (force) refreshAfterFlight.current = true;
        return;
      }
      void refresh(true);
    };
    stopLive.current = connectLiveTasks({
      message: message => {
        if (!active.current || expired.current) return;
        wantedRevision.current = Math.max(wantedRevision.current, message.revision);
        if ('live' in message) {
          liveAvailable.current = message.live;
          if (message.type === 'ready') liveReady.current = true;
          const force = message.type === 'ready' || message.type === 'resync';
          if (force) syncCycle.current += 1;
          const caughtUp = !latest.current.loading && !latest.current.needsResync && latest.current.revision >= wantedRevision.current;
          commit(value => ({ ...value, needsResync: force || value.needsResync, connection: !navigator.onLine ? 'offline' : liveReady.current && message.live && caughtUp && !force ? 'live' : 'reconnecting' }));
          if (force || !caughtUp || latest.current.error) refreshWhenIdle(force);
        } else if (message.revision > latest.current.revision) {
          commit(value => ({ ...value, needsResync: true, connection: navigator.onLine ? 'reconnecting' : 'offline' }));
          refreshWhenIdle();
        }
      },
      unavailable: () => {
        liveReady.current = false;
        liveAvailable.current = false;
        commit(value => ({ ...value, connection: navigator.onLine ? 'reconnecting' : 'offline' }));
        refreshWhenIdle();
      },
      expired: () => fail(new TaskAPIError(401, 'session_expired', 'Your session has expired. Open a fresh link using /profile in Telegram.')),
    });
    const offline = () => {
      refreshController.current?.abort();
      commit(value => ({ ...value, connection: 'offline', loading: false, refreshing: false }));
    };
    const focus = () => { if (document.visibilityState !== 'hidden') void refresh(true); };
    window.addEventListener('offline', offline);
    window.addEventListener('focus', focus);
    return () => {
      active.current = false;
      stopLive.current?.();
      stopLive.current = undefined;
      generation.current += 1;
      refreshController.current?.abort();
      controllers.current.forEach(controller => controller.abort());
      controllers.current.clear();
      newTimers.current.forEach(timer => window.clearTimeout(timer));
      newTimers.current.clear();
      window.clearTimeout(noticeTimer.current);
      noticeTimer.current = undefined;
      window.removeEventListener('offline', offline);
      window.removeEventListener('focus', focus);
    };
  }, [session.csrf_token, refresh, commit, fail]);

  const startWrite = useCallback((key: string, taskId?: string): number | null => {
    if (!active.current || expired.current || locks.current.has(key)) return null;
    if (!navigator.onLine) {
      commit(value => ({ ...value, connection: 'offline', error: 'You are offline. Reconnect before making changes.' }));
      return null;
    }
    locks.current.add(key);
    epoch.current += 1;
    refreshNumber.current += 1;
    refreshController.current?.abort();
    refreshController.current = null;
    commit(value => ({ ...value, error: null, refreshing: false,
      pendingIds: taskId ? new Set([...value.pendingIds, taskId]) : value.pendingIds,
    }));
    return generation.current;
  }, [commit]);
  const finishWrite = useCallback((key: string, started: number, taskId?: string) => {
    if (!valid(started)) return;
    locks.current.delete(key);
    epoch.current += 1;
    commit(value => ({ ...value, pendingIds: taskId ? new Set([...value.pendingIds].filter(id => id !== taskId)) : value.pendingIds }));
    if (!locks.current.size && refreshAfterWrite.current) {
      refreshAfterWrite.current = false;
      void refresh(true);
    }
  }, [commit, refresh, valid]);

  const getTask = useCallback(async (id: string): Promise<Task | null> => {
    const started = generation.current;
    if (!valid(started) || tombstones.current.has(id)) return null;
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      const task = await taskRequest<Task>(`/api/tasks/${encodeURIComponent(id)}`, session, { signal: controller.signal });
      if (!valid(started) || tombstones.current.has(id)) return null;
      const previous = latest.current.tasks.find(item => item.id === id);
      if (previous && (previous.version > task.version || (latest.current.pendingIds.has(id) && previous.version === task.version))) return previous;
      if (!previous || task.version > previous.version) epoch.current += 1;
      commit(value => ({ ...value, tasks: mergeTask(value.tasks, task) }));
      return task;
    } catch (error) {
      if (!valid(started)) return null;
      if (error instanceof TaskAPIError && error.status === 404) {
        tombstones.current.add(id);
        epoch.current += 1;
        commit(value => ({ ...value, tasks: value.tasks.filter(item => item.id !== id) }));
        showNotice('That task is no longer available.');
      }
      fail(error);
      return null;
    } finally { controllers.current.delete(controller); }
  }, [session.csrf_token, commit, fail, showNotice, valid]);

  const changeStatus = useCallback(async (id: string, status: TaskStatus): Promise<boolean> => {
    const before = latest.current.tasks.find(task => task.id === id);
    if (!before || !['pending', 'in_progress', 'completed'].includes(status)) return false;
    if (before.status === status) return true;
    const started = startWrite(id, id);
    if (started === null) return false;
    commit(value => ({ ...value, tasks: mergeTask(value.tasks, { ...before, status }) }));
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      const task = await taskRequest<Task>(`/api/tasks/${encodeURIComponent(id)}`, session, {
        method: 'PATCH', headers: { 'If-Match': String(before.version) },
        body: JSON.stringify({ status }), signal: controller.signal,
      });
      if (!valid(started)) return false;
      if (!tombstones.current.has(id)) {
        commit(value => ({ ...value, tasks: mergeTask(value.tasks, task) }));
        showNotice(`Task moved to ${statusLabels[task.status]}.`);
      }
      refreshAfterWrite.current = true;
      return true;
    } catch (error) {
      if (!valid(started)) return false;
      // A newer event or detail response wins over the pre-mutation rollback.
      commit(value => ({ ...value, tasks: value.tasks.map(task => task.id === id && task.version === before.version && task.status === status ? before : task) }));
      fail(error);
      refreshAfterWrite.current = true;
      if (error instanceof TaskAPIError && [404, 409].includes(error.status)) {
        await getTask(id);
      }
      return false;
    } finally {
      controllers.current.delete(controller);
      finishWrite(id, started, id);
    }
  }, [session.csrf_token, commit, fail, finishWrite, getTask, showNotice, startWrite, valid]);

  const createTask = useCallback(async (content: string, key: string): Promise<Task | null> => {
    const lock = `create:${key}`;
    const started = startWrite(lock);
    if (started === null) return null;
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      const task = await taskRequest<Task>('/api/tasks', session, {
        method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ content }), signal: controller.signal,
      });
      if (!valid(started)) return null;
      if (!tombstones.current.has(task.id)) {
        commit(value => ({ ...value, tasks: mergeTask(value.tasks, task) }));
        showNotice('Task added to Pending.');
        highlight([task.id]);
      }
      refreshAfterWrite.current = true;
      return task;
    } catch (error) {
      if (valid(started)) fail(error);
      return null;
    } finally {
      controllers.current.delete(controller);
      finishWrite(lock, started);
    }
  }, [session.csrf_token, commit, fail, finishWrite, highlight, showNotice, startWrite, valid]);

  const deleteTask = useCallback(async (id: string): Promise<boolean> => {
    const before = latest.current.tasks.find(task => task.id === id);
    if (!before) return tombstones.current.has(id);
    const started = startWrite(id, id);
    if (started === null) return false;
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      try {
        await taskRequest<void>(`/api/tasks/${encodeURIComponent(id)}`, session, {
          method: 'DELETE', headers: { 'If-Match': String(before.version) }, signal: controller.signal,
        });
      } catch (error) {
        if (!(error instanceof TaskAPIError && error.status === 404)) throw error;
      }
      if (!valid(started)) return false;
      tombstones.current.add(id);
      commit(value => ({ ...value, tasks: value.tasks.filter(task => task.id !== id) }));
      showNotice('Task deleted.');
      refreshAfterWrite.current = true;
      return true;
    } catch (error) {
      if (!valid(started)) return false;
      fail(error);
      refreshAfterWrite.current = true;
      if (error instanceof TaskAPIError && error.status === 409) {
        await getTask(id);
      }
      return false;
    } finally {
      controllers.current.delete(controller);
      finishWrite(id, started, id);
    }
  }, [session.csrf_token, commit, fail, finishWrite, getTask, showNotice, startWrite, valid]);

  const applyEvent = useCallback((event: TaskEvent) => {
    if (!active.current || expired.current) return;
    const previous = latest.current;
    const next = reduceTaskEvent(previous, event);
    if (next === previous) return;
    epoch.current += 1;
    if (!next.needsResync && event.type === 'task.deleted') tombstones.current.add(event.task_id);
    commit(value => ({ ...value, ...next }));
    if (next.needsResync) void refresh(true);
    else if (event.type === 'task.created') {
      highlight([event.task.id]);
      showNotice('A new task was added.');
    }
  }, [commit, highlight, refresh, showNotice]);

  const clearError = useCallback(() => { commit(value => ({ ...value, error: null })); }, [commit]);
  return { ...state, refresh, changeStatus, createTask, deleteTask, getTask, applyEvent, clearError };
}
