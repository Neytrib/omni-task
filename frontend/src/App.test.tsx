import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App, ConnectionIndicator } from './App';
import { bootstrapAuth } from './auth';
import type { Task } from './tasks';

const token = 'synthetic-token-no-real-credential';
const session = { csrf_token: 'synthetic-csrf', expires_at: '2030-01-01T00:00:00Z' };
const task: Task = { id: '10000000-0000-0000-0000-000000000001', owner_id: 'test-owner', title: 'Review the application', content: 'Review the application\nKeep the final line intact.', source: 'telegram_voice', status: 'pending', version: 1, created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:00Z' };
function reply(status: number, data: unknown = {}) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
}
function api(items: Task[] = []) {
  const request = vi.fn((path: string, options?: RequestInit) => {
    if (path.startsWith('/api/tasks?')) return reply(200, { items, next_cursor: null, revision: 1 });
    if (path === '/api/session') return reply(200, session);
    if (path === '/api/auth/logout') return reply(204);
    if (path === `/api/tasks/${task.id}` && !options?.method) return reply(200, items[0] ?? task);
    return reply(200);
  });
  vi.stubGlobal('fetch', request); return request;
}
function signedIn() { return Promise.resolve({ state: 'signed-in' as const, session }); }

describe('private dashboard', () => {
  it('removes credentials before requests and exchanges once under StrictMode', async () => {
    window.history.replaceState(null, '', `/login#token=${token}`);
    const request = api();
    request.mockImplementationOnce(() => { expect(window.location.hash).toBe(''); return reply(200); });
    render(<StrictMode><App authentication={bootstrapAuth()} /></StrictMode>);
    expect(await screen.findByRole('button', { name: 'Log out' })).toBeEnabled();
    await screen.findByText('Nothing waiting.');
    const exchanges = request.mock.calls.filter(([url]) => url === '/api/auth/exchange');
    expect(exchanges).toHaveLength(1);
    expect(exchanges[0][1]?.body).toBe(JSON.stringify({ token }));
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
    expect(document.body.textContent).not.toContain(token);
  });
  it.each([401, 422])('handles invalid, used, or expired links (%i)', async status => {
    window.history.replaceState(null, '', `/login#token=${token}`);
    const request = vi.fn(() => reply(status)); vi.stubGlobal('fetch', request);
    render(<App authentication={bootstrapAuth()} />);
    expect(await screen.findByText(/login link is invalid or expired/)).toBeInTheDocument();
    expect(screen.getByText('/profile')).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'New task' })).not.toBeInTheDocument();
  });
  it('distinguishes exchange service failures from invalid credentials', async () => {
    window.history.replaceState(null, '', `/login#token=${token}`);
    vi.stubGlobal('fetch', vi.fn(() => reply(503)));
    render(<App authentication={bootstrapAuth()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('could not open your workspace');
    expect(window.location.hash).toBe('');
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument();
  });
  it('explains Telegram access without a registration form', async () => {
    vi.stubGlobal('fetch', vi.fn(() => reply(401)));
    render(<App authentication={bootstrapAuth()} />);
    expect(await screen.findByText(/Open a fresh private login link/)).toBeInTheDocument();
    expect(screen.getByText('Tap Open Dashboard')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
  it('renders actual loaded tasks, source, counts, and all three columns', async () => {
    api([task]); render(<App authentication={signedIn()} />);
    expect(await screen.findByRole('button', { name: task.title })).toBeInTheDocument();
    const pending = screen.getByRole('region', { name: /Pending/ });
    expect(within(pending).getByLabelText('1 tasks')).toHaveTextContent('1');
    expect(within(pending).getByText('Voice note')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /In Progress/ })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /Completed/ })).toBeInTheDocument();
    expect(screen.getByText('Live')).toBeInTheDocument();
  });
  it('opens complete long content as safe text and returns focus', async () => {
    const long = { ...task, content: '<script>window.UNSAFE=true</script>\n' + 'Full line.\n'.repeat(700) + 'LAST LINE' };
    api([long]); render(<App authentication={signedIn()} />);
    const opener = await screen.findByRole('button', { name: task.title }); opener.focus(); fireEvent.click(opener);
    const dialog = await screen.findByRole('dialog', { name: task.title });
    await waitFor(() => expect(dialog).toHaveAttribute('aria-busy', 'false'));
    expect(within(dialog).getByRole('region', { name: 'Complete task content' }).textContent).toContain(long.content);
    expect(dialog.querySelector('script')).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(opener).toHaveFocus());
  });
  it('changes status with the accessible selector through the real API contract', async () => {
    const request = api([task]);
    request.mockImplementation((path, options) => {
      if (path === '/api/session') return reply(200, session);
      if (options?.method === 'PATCH') return reply(200, { ...task, status: 'in_progress', version: 2 });
      if (path.startsWith('/api/tasks?')) return reply(200, { items: [task], next_cursor: null, revision: 1 });
      return reply(200);
    });
    render(<App authentication={signedIn()} />);
    fireEvent.change(await screen.findByLabelText(`Status for ${task.title}`), { target: { value: 'in_progress' } });
    await waitFor(() => expect(within(screen.getByRole('region', { name: /In Progress/ })).getByRole('button', { name: task.title })).toBeInTheDocument());
    const call = request.mock.calls.find(([, options]) => options?.method === 'PATCH')!;
    expect(call[0]).toBe(`/api/tasks/${task.id}`);
    expect(JSON.parse(call[1]?.body as string)).toEqual({ status: 'in_progress' });
    expect(new Headers(call[1]?.headers).get('If-Match')).toBe('1');
    expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe(session.csrf_token);
  });
  it('rolls a failed move back and exposes an actionable error', async () => {
    const request = api([task]);
    request.mockImplementation((path, options) => path === '/api/session' ? reply(200, session) : options?.method === 'PATCH' ? reply(503) : path.startsWith('/api/tasks?') ? reply(200, { items: [task], next_cursor: null, revision: 1 }) : reply(200, task));
    render(<App authentication={signedIn()} />);
    fireEvent.change(await screen.findByLabelText(`Status for ${task.title}`), { target: { value: 'completed' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('could not complete');
    expect(within(screen.getByRole('region', { name: /Pending/ })).getByRole('button', { name: task.title })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
  });
  it('creates through the API and shows new-task feedback without sample content', async () => {
    const request = api(); let items: Task[] = [];
    request.mockImplementation((path, options) => {
      if (path === '/api/session') return reply(200, session);
      if (path === '/api/tasks' && options?.method === 'POST') { const content = JSON.parse(options.body as string).content; items = [{ ...task, content, title: content, source: 'dashboard' }]; return reply(201, items[0]); }
      if (path.startsWith('/api/tasks?')) return reply(200, { items, next_cursor: null, revision: 1 });
      return reply(200);
    });
    render(<App authentication={signedIn()} />); await screen.findByText('Nothing waiting.');
    fireEvent.click(screen.getByRole('button', { name: 'New task' }));
    fireEvent.change(screen.getByLabelText('What needs doing?'), { target: { value: 'New real submission' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    expect(await screen.findByRole('button', { name: 'New real submission' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'New task' })).not.toBeInTheDocument();
    const call = request.mock.calls.find(([,options]) => options?.method === 'POST')!;
    expect(new Headers(call[1]?.headers).get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
  });
  it('persists only theme preference across renders', async () => {
    api(); const result = render(<App authentication={signedIn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Switch to dark theme' }));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(localStorage.getItem('omni-theme')).toBe('dark'); expect(localStorage.length).toBe(1);
    result.unmount(); render(<App authentication={signedIn()} />);
    expect(await screen.findByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument();
  });
  it('revokes the session with CSRF and removes private task content on logout', async () => {
    const request = api([task]); render(<App authentication={signedIn()} />);
    await screen.findByRole('button', { name: task.title });
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    expect(await screen.findByText(/Signed out/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: task.title })).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token } }));
  });
  it('keeps logout available after a connection failure', async () => {
    const request = api(); request.mockImplementation(path => path === '/api/session' ? reply(200, session) : path === '/api/auth/logout' ? Promise.reject(new Error('Synthetic offline')) : reply(200,{items:[],next_cursor:null,revision:0}));
    render(<App authentication={signedIn()} />); await screen.findByText('Nothing waiting.');
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not sign out');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Log out' })).toBeEnabled());
  });
  it('clears the board at the session deadline', async () => {
    vi.useFakeTimers(); api();
    try {
      await act(async () => { render(<App authentication={Promise.resolve({ state:'signed-in', session:{...session,expires_at:new Date(Date.now()+1000).toISOString()} })} />); });
      await act(async () => { vi.advanceTimersByTime(1001); });
      expect(screen.getByText(/Your session has expired/)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'New task' })).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });
  it('clears tasks and open drafts when another tab changes the session', async () => {
    let changed = false;
    const request = api([task]);
    request.mockImplementation(path => path === '/api/session' ? reply(200, changed ? { ...session, csrf_token: 'second-session' } : session) : reply(200, { items: [task], revision: 5, next_cursor: null }));
    render(<App authentication={signedIn()} />);
    await screen.findByRole('button', { name: task.title });
    fireEvent.click(screen.getByRole('button', { name: 'New task' }));
    fireEvent.change(screen.getByLabelText('What needs doing?'), { target: { value: 'Private unsaved draft' } });
    changed = true; fireEvent.focus(window);
    expect(await screen.findByText(/session changed in another tab/)).toBeInTheDocument();
    expect(screen.queryByDisplayValue('Private unsaved draft')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: task.title })).not.toBeInTheDocument();
  });
  it('rechecks long sessions after the maximum browser timer interval', async () => {
    vi.useFakeTimers(); api();
    try {
      const thirtyDays = 30 * 24 * 60 * 60 * 1000;
      await act(async () => { render(<App authentication={Promise.resolve({ state: 'signed-in', session: { ...session, expires_at: new Date(Date.now() + thirtyDays).toISOString() } })} />); });
      await act(async () => { vi.advanceTimersByTime(2_147_483_647); });
      expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument();
      await act(async () => { vi.advanceTimersByTime(thirtyDays - 2_147_483_647 + 1); });
      expect(screen.getByText(/Your session has expired/)).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });
  it.each(['connecting', 'reconnecting', 'offline', 'live'])('prepares a visible %s connection state', state => {
    render(<ConnectionIndicator state={state} />);
    expect(screen.getByRole('status')).toHaveTextContent(new RegExp(state,'i'));
  });
});
