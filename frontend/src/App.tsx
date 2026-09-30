import { useCallback, useEffect, useRef, useState, type MouseEvent } from 'react';
import { DragDropProvider, useDraggable, useDroppable } from '@dnd-kit/react';
import type { DragEndEvent } from '@dnd-kit/react';
import { Feedback, PointerSensor } from '@dnd-kit/dom';
import type { AuthResult, Session } from './auth';
import { apiFetch } from './transport';
import { useTasks } from './useTasks';
import { taskPointerSensor } from './cardDrag';
import { useBoardMotion } from './boardMotion';
import type { Task, TaskStatus } from './tasks';
import { NewTaskDialog, TaskDetailsDialog } from './components/TaskDialogs';

const statuses: TaskStatus[] = ['pending', 'in_progress', 'completed'];
const labels: Record<TaskStatus, string> = { pending: 'Pending', in_progress: 'In Progress', completed: 'Completed' };
const sourceLabels: Record<Task['source'], string> = { telegram_text: 'Telegram', telegram_voice: 'Voice note', dashboard: 'Dashboard' };
type IconName = 'plus' | 'sun' | 'moon' | 'logout' | 'refresh' | 'grid' | 'arrow' | 'lock' | 'grip' | 'mic' | 'message' | 'document' | 'check' | 'close';
function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  const paths: Record<IconName, React.ReactNode> = {
    plus: <path d="M12 5v14M5 12h14" />,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" /></>,
    moon: <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z" />,
    logout: <><path d="M9 4H4v16h5M9 12h12m-4-4 4 4-4 4" /></>,
    refresh: <><path d="M20 8V3m0 5h-5M4 16v5m0-5h5" /><path d="M5.1 8a7.5 7.5 0 0 1 12.3-3L20 8M4 16l2.6 3A7.5 7.5 0 0 0 19 16" /></>,
    grid: <><rect x="3" y="4" width="5" height="16" rx="1" /><rect x="10" y="4" width="5" height="11" rx="1" /><path d="M18 4h3v16h-3" /></>,
    arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3" /></>,
    grip: <><path d="M9 5h.01M15 5h.01M9 12h.01M15 12h.01M9 19h.01M15 19h.01" strokeWidth="3" /></>,
    mic: <><rect x="9" y="2" width="6" height="13" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" /></>,
    message: <path d="M20 4H4v13h4v4l5-4h7Z" />,
    document: <><path d="M14 3H5v18h14V8Z M14 3v5h5M8 12h8M8 16h6" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
  };
  return <svg className={`icon ${className}`} width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
function Brand() {
  return <div className="brand" aria-label="Omni Task"><span className="brand-mark" aria-hidden="true"><span /><span /><span /></span><span>omni<span className="brand-light">task</span></span></div>;
}
function useTheme() {
  const [preference, setPreference] = useState<'light' | 'dark' | null>(() => {
    try { const saved = localStorage.getItem('omni-theme'); return saved === 'light' || saved === 'dark' ? saved : null; } catch { return null; }
  });
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-color-scheme: dark)');
    const update = () => setSystemDark(query?.matches ?? false);
    query?.addEventListener('change', update);
    return () => query?.removeEventListener('change', update);
  }, []);
  const theme = preference ?? (systemDark ? 'dark' : 'light');
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const toggle = () => {
    const next = theme === 'light' ? 'dark' : 'light';
    setPreference(next);
    try { localStorage.setItem('omni-theme', next); } catch { /* Theme still works in memory. */ }
  };
  return { theme, toggle };
}
function ThemeButton({ theme, toggle }: ReturnType<typeof useTheme>) {
  return <button type="button" className="icon-button" aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`} title={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`} onClick={toggle}><Icon name={theme === 'light' ? 'moon' : 'sun'} /></button>;
}
function createdDate(value: string) {
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', ...(date.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' as const } : {}) }).format(date);
}

export function ConnectionIndicator({ state }: { state: string }) {
  const copy: Record<string, string> = { connecting: 'Connecting', ready: 'Reconnecting', live: 'Live', reconnecting: 'Reconnecting', offline: 'Offline', error: 'Connection interrupted' };
  return <span className={`connection connection-${state}`} role="status" title={state === 'reconnecting' || state === 'ready' ? 'Restoring live updates. You can refresh to load the latest tasks.' : undefined}><span className="connection-dot" aria-hidden="true" />{copy[state] ?? 'Reconnecting'}</span>;
}
function TaskCard({ task, busy, disabled, fresh, onOpen, onStatus }: {
  task: Task; busy: boolean; disabled: boolean; fresh: boolean; onOpen: (id: string, event: MouseEvent<HTMLButtonElement>) => void; onStatus: (id: string, status: TaskStatus) => void;
}) {
  const { ref, handleRef, isDragging } = useDraggable({ id: task.id, type: 'task', disabled: disabled || busy });
  const preview = Array.from(task.content).slice(0, 220).join('');
  return <article ref={ref} className={`task-card ${isDragging ? 'is-dragging' : ''} ${fresh ? 'is-new' : ''}`} aria-busy={busy} data-task-id={task.id} data-task-status={task.status} data-drag-disabled={disabled || busy}>
    <div className="task-card-top"><span className={`task-source source-${task.source}`}><Icon name={task.source === 'telegram_voice' ? 'mic' : task.source === 'telegram_text' ? 'message' : 'document'} />{sourceLabels[task.source]}</span><span className="card-top-actions">{fresh && <span className="new-marker" aria-hidden="true">New</span>}<button type="button" ref={handleRef} className="drag-handle" disabled={disabled || busy} aria-label={`Move ${task.title}`} title="Drag anywhere on the card"><Icon name="grip" /></button></span></div>
    <h3><button type="button" className="task-title" data-task-open onClick={event => onOpen(task.id, event)}>{task.title}</button></h3>
    <p className="task-preview">{preview}{Array.from(task.content).length > 220 ? '…' : ''}</p>
    <div className="task-card-bottom"><time dateTime={task.created_at} title={new Date(task.created_at).toLocaleString()}>{createdDate(task.created_at)}</time><label data-no-drag className={`card-status status-${task.status}`}><span className="sr-only">Status for {task.title}</span><select data-status-id={task.id} value={task.status} disabled={disabled || busy} onChange={event => onStatus(task.id, event.target.value as TaskStatus)}>{statuses.map(status => <option key={status} value={status}>{labels[status]}</option>)}</select></label></div>
    {busy && <span className="card-saving">Saving…</span>}
  </article>;
}
function TaskColumn({ status, tasks, loading, disabled, pendingIds, newTaskIds, onOpen, onStatus, onCreate }: {
  status: TaskStatus; tasks: Task[]; loading: boolean; disabled: boolean; pendingIds: Set<string>; newTaskIds: Set<string>; onOpen: (id: string, event: MouseEvent<HTMLButtonElement>) => void; onStatus: (id: string, status: TaskStatus) => void; onCreate: () => void;
}) {
  const { ref, isDropTarget } = useDroppable({ id: status, accept: 'task', disabled: disabled || loading });
  const empty = { pending: ['Nothing waiting.', 'New tasks start here.'], in_progress: ['Room to focus.', 'Move a task here when you begin.'], completed: ['A place for finished work.', 'Completed tasks will appear here.'] };
  return <section ref={ref} className={`task-column column-${status} ${isDropTarget ? 'is-drop-target' : ''}`} aria-labelledby={`column-${status}`}>
    <header className="column-heading"><h2 id={`column-${status}`}><span className="status-symbol" aria-hidden="true">{status === 'completed' ? <Icon name="check" /> : null}</span>{labels[status]}<span className="column-count" aria-label={`${tasks.length} tasks`}>{loading ? '–' : tasks.length}</span></h2>{status === 'pending' && <button className="column-add icon-button" type="button" aria-label="Add a pending task" disabled={disabled} onClick={onCreate}><Icon name="plus" /></button>}</header>
    <div className="column-content">
      {loading ? <div aria-hidden="true" className="skeleton-stack"><div className="skeleton-card"><i /><i /><i /></div><div className="skeleton-card"><i /><i /></div></div> : tasks.map(task => <TaskCard key={task.id} task={task} busy={pendingIds.has(task.id)} disabled={disabled} fresh={newTaskIds.has(task.id)} onOpen={onOpen} onStatus={onStatus} />)}
      {!loading && tasks.length === 0 && <div className="column-empty"><span className="empty-mark" aria-hidden="true">{status === 'pending' ? '01' : status === 'in_progress' ? '02' : '03'}</span><p>{empty[status][0]}</p><span>{empty[status][1]}</span>{status === 'pending' && <button className="text-button" type="button" disabled={disabled} onClick={onCreate}>Create a task <Icon name="arrow" /></button>}</div>}
    </div>
    <div className="drop-target-hint" aria-hidden="true"><Icon name="arrow" />Drop to mark {labels[status]}</div>
  </section>;
}
function Dashboard({ session, onExpired }: { session: Session; onExpired: (message?: string) => void }) {
  const board = useTasks(session, onExpired);
  const motion = useBoardMotion(board.tasks);
  const [dragging, setDragging] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const draggedTaskId = useRef<string | null>(null);
  const selectedRef = useRef<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const task = board.tasks.find(item => item.id === selected) ?? null;
  const disabled = board.connection === 'offline' || board.loading;
  const closeDetails = () => { selectedRef.current = null; setSelected(null); setDetailLoading(false); };
  useEffect(() => {
    if (selected && !task && !detailLoading && !board.loading) closeDetails();
  }, [selected, task, detailLoading, board.loading]);
  const openTask = (id: string, event: MouseEvent<HTMLButtonElement>) => {
    // A pointer release after dragging the title must not also open its dialog.
    // A fresh pointer-down resets this guard; keyboard activation remains available.
    if (event.defaultPrevented || (event.detail > 0 && draggedTaskId.current === id)) return;
    selectedRef.current = id; setSelected(id); setDetailLoading(true);
    void board.getTask(id).then(result => { if (selectedRef.current === id) { setDetailLoading(false); if (!result && !board.tasks.some(item => item.id === id)) closeDetails(); } });
  };
  function focusStatus(id: string) {
    requestAnimationFrame(() => {
      if (document.activeElement === document.body) Array.from(document.querySelectorAll<HTMLSelectElement>('[data-status-id]')).find(node => node.dataset.statusId === id)?.focus();
    });
  }
  function changeStatus(id: string, status: TaskStatus) { void board.changeStatus(id, status).finally(() => focusStatus(id)); focusStatus(id); }
  function onDragEnd(event: DragEndEvent) {
    const { source, target } = event.operation;
    motion.finishDrag(event.canceled || !source ? null : String(source.id), source?.element);
    setDragging(false);
    if (event.canceled) return;
    if (source && target && statuses.includes(String(target.id) as TaskStatus)) changeStatus(String(source.id), String(target.id) as TaskStatus);
  }
  return <main className="workspace" id="main-content" onPointerDownCapture={event => { if (event.isPrimary && event.button === 0) draggedTaskId.current = null; }}>
    <div className="workspace-heading"><div><p className="eyebrow"><span className="workspace-mark" aria-hidden="true" />PERSONAL WORKSPACE</p><h1>Your tasks<span className="heading-stop">.</span></h1><p className="workspace-description">From first thought to finished.</p></div><button className="button button-primary new-task-button" data-dialog-return-focus type="button" onClick={() => setCreateOpen(true)} disabled={disabled}><Icon name="plus" />New task</button></div>
    <div className="board-toolbar"><div className="board-view"><Icon name="grid" /><span>Board</span><span className="total-count">{board.loading ? 'Loading tasks' : `${board.tasks.length} ${board.tasks.length === 1 ? 'task' : 'tasks'}`}</span></div><span className="board-hint"><Icon name="grip" />Drag a card to change its status</span><div className="board-connection"><ConnectionIndicator state={board.refreshing && board.connection === 'ready' ? 'connecting' : board.connection} /><button className="icon-button refresh-button" type="button" onClick={() => { void board.refresh(); }} disabled={board.refreshing || board.connection === 'offline'} aria-label="Refresh tasks" title="Refresh tasks"><Icon name="refresh" className={board.refreshing ? 'is-spinning' : ''} /></button></div></div>
    {board.connection === 'offline' && <div className="notice notice-warning" role="status">You’re offline. These are your last loaded tasks. Reconnect to make changes.</div>}
    {board.error && <div className="notice notice-error" role="alert"><span>{board.error}</span><div><button type="button" className="text-button" onClick={() => { void board.refresh(); }} disabled={board.refreshing || board.connection === 'offline'}>Try again</button><button type="button" className="icon-button" onClick={board.clearError} aria-label="Dismiss error"><Icon name="close" /></button></div></div>}
    {board.loading && <p className="sr-only" role="status">Loading your tasks…</p>}
    <DragDropProvider sensors={defaults => [...defaults.filter(sensor => sensor !== PointerSensor), taskPointerSensor]} onDragStart={event => { draggedTaskId.current = event.operation.source ? String(event.operation.source.id) : null; if (draggedTaskId.current) motion.beginDrag(draggedTaskId.current); setDragging(true); }} plugins={defaults => [...defaults, Feedback.configure({ dropAnimation: null })]} onDragEnd={onDragEnd}><div ref={motion.ref} className={`board ${dragging ? 'is-board-dragging' : ''}`}>{statuses.map(status => <TaskColumn key={status} status={status} tasks={board.tasks.filter(task => task.status === status)} loading={board.loading} disabled={disabled} pendingIds={board.pendingIds} newTaskIds={board.newTaskIds} onOpen={openTask} onStatus={changeStatus} onCreate={() => setCreateOpen(true)} />)}</div></DragDropProvider>
    <footer className="workspace-footer"><span><Icon name="lock" />Only you can see these tasks</span><span>Capture in Telegram. Organize here.</span></footer>
    <div className="toast-region" aria-live="polite" aria-atomic="true">{board.notice && <div className="toast"><Icon name="check" />{board.notice}</div>}</div>
    <NewTaskDialog open={createOpen} onClose={() => setCreateOpen(false)} onCreate={board.createTask} disabled={disabled} />
    <TaskDetailsDialog task={task} open={selected !== null} onClose={closeDetails} onStatusChange={board.changeStatus} onDelete={board.deleteTask} pending={selected ? board.pendingIds.has(selected) : false} disabled={disabled} loading={detailLoading} />
  </main>;
}

export function App({ authentication }: { authentication: Promise<AuthResult> }) {
  const [auth, setAuth] = useState<AuthResult | { state: 'loading' }>({ state: 'loading' });
  const [busy, setBusy] = useState(false);
  const [logoutError, setLogoutError] = useState('');
  const theme = useTheme();
  const expire = useCallback((message?: string) => setAuth({ state: 'signed-out', message: message ?? 'Your session has expired. Send /profile to your Telegram bot for a fresh private link.' }), []);
  useEffect(() => { let active = true; void authentication.then(result => { if (active) setAuth(result); }); return () => { active = false; }; }, [authentication]);
  useEffect(() => {
    if (auth.state !== 'signed-in') return;
    const expiresAt = new Date(auth.session.expires_at).getTime();
    let timer = 0;
    const check = () => {
      const remaining = expiresAt - Date.now();
      if (remaining <= 0 || !Number.isFinite(remaining)) { expire(); return; }
      timer = window.setTimeout(check, Math.min(remaining, 2_147_483_647));
    };
    check();
    return () => window.clearTimeout(timer);
  }, [auth, expire]);
  async function logout() {
    if (auth.state !== 'signed-in' || busy) return;
    setBusy(true); setLogoutError('');
    try {
      const response = await apiFetch('/api/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': auth.session.csrf_token } });
      if (!response.ok && response.status !== 401) throw new Error('Logout failed');
      setAuth({ state: 'signed-out', message: 'Signed out. Send /profile to your Telegram bot to return with a fresh private link.' });
    } catch { setLogoutError('Could not sign out. Check your connection and try again.'); }
    finally { setBusy(false); }
  }
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <header className="app-header"><div className="header-identity"><Brand /><span className="workspace-badge"><Icon name="lock" />Personal</span></div><div className="header-actions"><ThemeButton {...theme} />{auth.state === 'signed-in' && <><span className="header-divider" /><button className="logout-button" type="button" disabled={busy} onClick={() => { void logout(); }}><Icon name="logout" />{busy ? 'Signing out…' : 'Log out'}</button></>}</div></header>
    {logoutError && <div className="logout-error notice notice-error" role="alert">{logoutError}</div>}
    {auth.state === 'signed-in' ? <Dashboard session={auth.session} onExpired={expire} /> : <main className="access-screen" id="main-content"><div className="access-kicker"><Icon name="lock" />PRIVATE BY DEFAULT</div><h1>{auth.state === 'loading' ? 'Opening your workspace…' : 'Open your private board.'}</h1>{auth.state === 'loading' ? <p role="status">Checking your private session…</p> : <><p className={`access-message ${auth.state === 'error' ? 'access-error' : ''}`} role={auth.state === 'error' ? 'alert' : undefined}>{auth.message}</p><div className="access-instructions"><span className="instruction-number">01</span><div><h2>Start in Telegram</h2><p>Open your chat with the bot and send <code>/profile</code>.</p></div><span className="instruction-number">02</span><div><h2>Tap Open Dashboard</h2><p>Your private link signs you in. No password or registration needed.</p></div></div>{auth.state === 'error' && <button className="button button-primary" type="button" onClick={() => window.location.reload()}>Try again <Icon name="refresh" /></button>}<p className="access-footnote">For your privacy, login links expire and can only be used once.</p></>}</main>}
  </div>;
}
