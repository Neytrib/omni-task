export type LiveTaskMessage =
  | { type: 'ready' | 'heartbeat' | 'resync'; revision: number; live: boolean }
  | { type: 'task_created' | 'task_updated' | 'task_deleted'; revision: number; task_id: string };

type Callbacks = {
  message: (message: LiveTaskMessage) => void;
  unavailable: () => void;
  expired: () => void;
};

export function parseLiveTaskMessage(data: unknown): LiveTaskMessage | null {
  if (typeof data !== 'string' || data.length > 2048) return null;
  let value: Record<string, unknown>;
  try { value = JSON.parse(data); } catch { return null; }
  if (!value || typeof value !== 'object' || !Number.isSafeInteger(value.revision) || Number(value.revision) < 0) return null;
  if (['ready', 'heartbeat', 'resync'].includes(String(value.type)) && typeof value.live === 'boolean') return value as LiveTaskMessage;
  if (['task_created', 'task_updated', 'task_deleted'].includes(String(value.type)) &&
      typeof value.task_id === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value.task_id)) return value as LiveTaskMessage;
  return null;
}

// Each board owns its connection. Only the HttpOnly session cookie authenticates
// it: no identity, cursor, login token, or CSRF secret belongs in this URL.
export function connectLiveTasks(callbacks: Callbacks) {
  const url = new URL('/api/ws/tasks', window.location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  let socket: WebSocket | null = null;
  let stopped = false;
  let attempts = 0;
  let retry: number | undefined;
  let watchdog: number | undefined;
  let fallback: number | undefined;
  let lastMessage = Date.now();

  function detach() {
    window.clearTimeout(watchdog);
    window.clearTimeout(fallback);
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close();
      socket = null;
    }
  }
  function schedule() {
    if (stopped || !navigator.onLine || retry !== undefined) return;
    const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempts++, 5) * (0.75 + Math.random() * 0.5));
    retry = window.setTimeout(() => { retry = undefined; open(); }, delay);
  }
  function lost() {
    detach();
    if (stopped) return;
    callbacks.unavailable();
    schedule();
  }
  function armWatchdog(milliseconds: number) {
    window.clearTimeout(watchdog);
    watchdog = window.setTimeout(lost, milliseconds);
  }
  function open() {
    if (stopped || !navigator.onLine || socket) return;
    try { socket = new WebSocket(url); } catch { lost(); return; }
    const current = socket;
    lastMessage = Date.now();
    // The API remains usable during a transport outage. A later ready message
    // always starts another snapshot, closing the subscription/load race.
    fallback = window.setTimeout(() => { if (socket === current) callbacks.unavailable(); }, 1500);
    armWatchdog(10_000);
    current.onmessage = event => {
      if (socket !== current || stopped) return;
      const message = parseLiveTaskMessage(event.data);
      if (!message) { lost(); return; }
      window.clearTimeout(fallback);
      lastMessage = Date.now();
      armWatchdog(45_000);
      if (message.type === 'ready' || message.type === 'heartbeat') attempts = 0;
      callbacks.message(message);
    };
    current.onerror = lost;
    current.onclose = event => {
      if (socket !== current || stopped) return;
      if (event.code === 4401) {
        stopped = true;
        detach();
        callbacks.expired();
      } else lost();
    };
  }
  function offline() {
    window.clearTimeout(retry); retry = undefined;
    detach();
    callbacks.unavailable();
  }
  function resume() {
    if (stopped || !navigator.onLine) return;
    if (socket && Date.now() - lastMessage > 45_000) detach();
    if (!socket) { window.clearTimeout(retry); retry = undefined; open(); }
  }
  function pageHide() { offline(); }
  window.addEventListener('offline', offline);
  window.addEventListener('online', resume);
  window.addEventListener('focus', resume);
  window.addEventListener('pageshow', resume);
  window.addEventListener('pagehide', pageHide);
  if (navigator.onLine) open(); else callbacks.unavailable();
  return () => {
    stopped = true;
    window.clearTimeout(retry);
    detach();
    window.removeEventListener('offline', offline);
    window.removeEventListener('online', resume);
    window.removeEventListener('focus', resume);
    window.removeEventListener('pageshow', resume);
    window.removeEventListener('pagehide', pageHide);
  };
}
