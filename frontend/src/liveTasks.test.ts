import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { connectLiveTasks, parseLiveTaskMessage } from './liveTasks';
import { TestWebSocket } from './testWebSocket';

beforeEach(() => { vi.useFakeTimers(); TestWebSocket.autoReady = false; });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
const callbacks = () => ({ message: vi.fn(), unavailable: vi.fn(), expired: vi.fn() });
const socket = () => TestWebSocket.instances.at(-1)!;

describe('bounded WebSocket recovery', () => {
  it('uses only the current origin and a fixed path, excluding page query/fragment credentials', () => {
    window.history.replaceState(null, '', '/login?token=synthetic#token=synthetic');
    const stop = connectLiveTasks(callbacks());
    expect(socket().url).toBe(`${window.location.origin.replace('http', 'ws')}/api/ws/tasks`);
    stop();
  });
  it('loads fallback API state while awaiting a subscription and bounds a stalled handshake', async () => {
    const events = callbacks(); const stop = connectLiveTasks(events);
    await vi.advanceTimersByTimeAsync(1500);
    expect(events.unavailable).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(8500);
    expect(socket().readyState).toBe(3);
    expect(events.unavailable).toHaveBeenCalledTimes(2);
    stop();
  });
  it('reconnects with jittered exponential delays capped at thirty seconds', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(1);
    const stop = connectLiveTasks(callbacks());
    for (const delay of [1250, 2500, 5000, 10000, 20000, 30000, 30000]) {
      const count = TestWebSocket.instances.length;
      socket().disconnect();
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(TestWebSocket.instances).toHaveLength(count);
      await vi.advanceTimersByTimeAsync(1);
      expect(TestWebSocket.instances).toHaveLength(count + 1);
    }
    stop(); expect(vi.getTimerCount()).toBe(0);
  });
  it('reconnects after a missing heartbeat and accepts heartbeat traffic as liveness', async () => {
    const stop = connectLiveTasks(callbacks());
    const initial = socket();
    initial.receive({ type: 'ready', revision: 0, live: true });
    await vi.advanceTimersByTimeAsync(30_000);
    initial.receive({ type: 'heartbeat', revision: 0, live: true });
    await vi.advanceTimersByTimeAsync(44_999);
    expect(initial.readyState).not.toBe(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(initial.readyState).toBe(3);
    await vi.advanceTimersByTimeAsync(1250);
    expect(TestWebSocket.instances).toHaveLength(2);
    stop();
  });
  it('stops retrying after authentication expiry and cleans all listeners/timers', async () => {
    const events = callbacks(); const stop = connectLiveTasks(events);
    socket().disconnect(4401);
    await vi.advanceTimersByTimeAsync(90_000);
    window.dispatchEvent(new Event('online'));
    expect(events.expired).toHaveBeenCalledOnce();
    expect(TestWebSocket.instances).toHaveLength(1);
    stop(); expect(vi.getTimerCount()).toBe(0);
  });
  it('stops in the background page cache and reconnects after pageshow', () => {
    const stop = connectLiveTasks(callbacks());
    const first = socket();
    window.dispatchEvent(new Event('pagehide'));
    expect(first.readyState).toBe(3);
    window.dispatchEvent(new Event('pageshow'));
    expect(TestWebSocket.instances).toHaveLength(2);
    stop();
  });
  it.each(['null', '{}', '[]', 'invalid', JSON.stringify({ type: 'task_created', task_id: 'forged', revision: 1 }), JSON.stringify({ type: 'ready', live: true, revision: -1 }), JSON.stringify({ type: 'heartbeat', live: true, revision: 1.5 })])('rejects malformed wire data %s', data => {
    expect(parseLiveTaskMessage(data)).toBeNull();
  });
});
