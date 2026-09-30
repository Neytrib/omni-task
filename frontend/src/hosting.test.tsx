import { StrictMode } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { publicConfig, contentSecurityPolicy } from '../publicConfig';
import { apiFetch, apiUrl, taskWebSocketUrl } from './transport';
import { bootstrapAuth } from './auth';
import { taskRequest } from './tasks';
import { connectLiveTasks } from './liveTasks';
import { TestWebSocket } from './testWebSocket';
import { App } from './App';

const apiOrigin = 'https://api.example.test';
const session = { csrf_token: 'synthetic-csrf', expires_at: '2099-01-01T00:00:00Z' };
const token = 'synthetic-login-token';
const response = (status: number, body: unknown = {}) => Promise.resolve(new Response(status === 204 ? null : JSON.stringify(body), { status }));
function hosted() {
  vi.stubEnv('VITE_API_ORIGIN', apiOrigin);
  vi.stubEnv('VITE_BASE_PATH', '/omni-task/');
}
afterEach(() => vi.unstubAllEnvs());

describe('public deployment settings', () => {
  it('preserves same-origin local transport and base by default', async () => {
    expect(publicConfig({})).toEqual({ basePath: '/', apiOrigin: '', webSocketOrigin: '' });
    const request = vi.fn().mockImplementation(() => response(200)); vi.stubGlobal('fetch', request);
    await apiFetch('/api/session');
    expect(request).toHaveBeenCalledWith('/api/session', expect.objectContaining({ credentials: 'same-origin' }));
    expect(taskWebSocketUrl().href).toBe(`${window.location.origin.replace(/^http/, 'ws')}/api/ws/tasks`);
  });

  it('derives WSS from the HTTPS API and normalizes an origin trailing slash', () => {
    expect(publicConfig({ VITE_API_ORIGIN: `${apiOrigin}/`, VITE_BASE_PATH: '/omni-task/' }))
      .toEqual({ basePath: '/omni-task/', apiOrigin, webSocketOrigin: 'wss://api.example.test' });
  });

  it.each([
    'http://api.example.test', 'javascript:alert(1)', '//api.example.test',
    'https://user:synthetic-password@api.example.test', 'https://api.example.test/api',
    'https://api.example.test?token=synthetic', 'https://api.example.test#synthetic',
    'https://api.example.test\\evil', ' https://api.example.test',
  ])('rejects unsafe public API origins without reflecting values: %s', value => {
    expect(() => publicConfig({ VITE_API_ORIGIN: value })).toThrow('VITE_API_ORIGIN must be a HTTPS origin');
    try { publicConfig({ VITE_API_ORIGIN: value }); } catch (error) { expect(String(error)).not.toContain(value); }
  });

  it.each(['omni-task', '/omni-task', '//evil.test/', '/foo/../', '/foo?token=synthetic/', '/foo/#token=synthetic/'])('rejects unsafe or ambiguous asset bases: %s', value => {
    expect(() => publicConfig({ VITE_API_ORIGIN: apiOrigin, VITE_BASE_PATH: value })).toThrow('VITE_BASE_PATH');
  });

  it('requires the API for a Pages base and refuses a separate cookie hostname for WSS', () => {
    expect(() => publicConfig({ VITE_BASE_PATH: '/omni-task/' })).toThrow('VITE_API_ORIGIN is required');
    expect(() => publicConfig({ VITE_API_ORIGIN: apiOrigin, VITE_WS_ORIGIN: 'wss://other.example.test' })).toThrow('API hostname');
    expect(() => publicConfig({ VITE_API_ORIGIN: apiOrigin, VITE_WS_ORIGIN: 'ws://api.example.test' })).toThrow('WSS origin');
    expect(() => publicConfig({ VITE_WS_ORIGIN: 'wss://api.example.test' })).toThrow('API hostname');
  });

  it('generates a narrow static CSP for the configured HTTPS and WSS endpoints', () => {
    const policy = contentSecurityPolicy(publicConfig({ VITE_API_ORIGIN: apiOrigin }));
    expect(policy).toContain("connect-src 'self' https://api.example.test wss://api.example.test;");
    expect(policy).toContain("script-src 'self';");
    expect(policy).toContain("base-uri 'none';");
    expect(policy).not.toMatch(/unsafe-inline|unsafe-eval|connect-src \*/);
  });
});

describe('split-host authentication and transport', () => {
  it('keeps the repository pathname, removes credentials before fetch, and verifies the cookie once', async () => {
    hosted(); window.history.replaceState(null, '', `/omni-task/?tracking=discard#token=${token}`);
    const request = vi.fn((url: string) => {
      expect(window.location.pathname).toBe('/omni-task/');
      expect(window.location.hash).toBe(''); expect(window.location.search).toBe('');
      return response(200, url.endsWith('/api/session') ? session : {});
    }); vi.stubGlobal('fetch', request);
    expect(await bootstrapAuth()).toEqual({ state: 'signed-in', session });
    expect(request.mock.calls.map(([url]) => url)).toEqual([`${apiOrigin}/api/auth/exchange`, `${apiOrigin}/api/session`]);
    for (const call of request.mock.calls) {
      const options = (call as unknown as [string, RequestInit])[1];
      expect(options.credentials).toBe('include'); expect(options.redirect).toBe('error');
      expect(options.referrerPolicy).toBe('no-referrer');
    }
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  });

  it('does not treat a successful token exchange as proof of a usable browser session', async () => {
    hosted(); window.history.replaceState(null, '', `/omni-task/#token=${token}`);
    const request = vi.fn().mockImplementationOnce(() => response(200, session)).mockImplementationOnce(() => response(401));
    vi.stubGlobal('fetch', request);
    const result = await bootstrapAuth();
    expect(result).toMatchObject({ state: 'signed-out', message: expect.stringContaining('browser could not keep your session') });
    expect(result).toMatchObject({ message: expect.stringContaining('fresh /profile link') });
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('makes a missing hosted API an actionable configuration error before any request', async () => {
    vi.stubEnv('VITE_BASE_PATH', '/omni-task/'); vi.stubEnv('VITE_API_ORIGIN', '');
    window.history.replaceState(null, '', `/omni-task/#token=${token}`);
    const request = vi.fn(); vi.stubGlobal('fetch', request);
    expect(await bootstrapAuth()).toMatchObject({ state: 'error', message: expect.stringContaining('VITE_API_ORIGIN') });
    expect(request).not.toHaveBeenCalled(); expect(window.location.hash).toBe('');
  });

  it('never exchanges a token supplied in a query or sends page credentials to WebSockets', async () => {
    hosted(); window.history.replaceState(null, '', `/omni-task/?token=${token}#ignored`);
    const request = vi.fn().mockImplementation(() => response(401)); vi.stubGlobal('fetch', request);
    await bootstrapAuth();
    expect(request).toHaveBeenCalledWith(`${apiOrigin}/api/session`, expect.objectContaining({ credentials: 'include' }));
    expect(request).toHaveBeenCalledTimes(1);
    const stop = connectLiveTasks({ message: vi.fn(), unavailable: vi.fn(), expired: vi.fn() });
    expect(TestWebSocket.instances.at(-1)?.url).toBe('wss://api.example.test/api/ws/tasks');
    stop();
  });

  it('preserves CSRF/version/idempotency headers and complete content with cross-origin cookies', async () => {
    hosted(); const request = vi.fn().mockImplementation(() => response(200)); vi.stubGlobal('fetch', request);
    const body = JSON.stringify({ content: ' Full original content\nwith trailing space ' });
    await taskRequest('/api/tasks', session, { method: 'POST', body, headers: { 'Idempotency-Key': 'synthetic-idempotency', 'If-Match': '2' } });
    const [url, options] = request.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${apiOrigin}/api/tasks`); expect(options.credentials).toBe('include');
    expect(options.body).toBe(body); const headers = new Headers(options.headers);
    expect(headers.get('X-CSRF-Token')).toBe(session.csrf_token);
    expect(headers.get('Idempotency-Key')).toBe('synthetic-idempotency'); expect(headers.get('If-Match')).toBe('2');
  });

  it.each(['https://evil.example/api/tasks', '//evil.example/api/tasks', '/api/../internal/users', '/api/%2e%2e/internal/users', '/internal/tasks', '/api/tasks#token=synthetic', '/api/\\evil'])('refuses an API path that could escape the intended transport: %s', path => {
    hosted(); expect(() => apiUrl(path)).toThrow('Invalid API request path');
  });

  it('exchanges only once under StrictMode and revokes the hosted cookie through logout', async () => {
    hosted(); window.history.replaceState(null, '', `/omni-task/#token=${token}`);
    const request = vi.fn((url: string) => {
      if (url === `${apiOrigin}/api/session`) return response(200, session);
      if (url.startsWith(`${apiOrigin}/api/tasks?`)) return response(200, { items: [], next_cursor: null, revision: 0 });
      return response(204);
    }); vi.stubGlobal('fetch', request);
    render(<StrictMode><App authentication={bootstrapAuth()} /></StrictMode>);
    await screen.findByText('Nothing waiting.');
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    await screen.findByText(/Signed out. Send \/profile/);
    expect(request.mock.calls.filter(([url]) => url.endsWith('/auth/exchange'))).toHaveLength(1);
    expect(request).toHaveBeenCalledWith(`${apiOrigin}/api/auth/logout`, expect.objectContaining({ credentials: 'include', method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token } }));
    expect(screen.queryByRole('button', { name: 'New task' })).not.toBeInTheDocument();
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  });
});
