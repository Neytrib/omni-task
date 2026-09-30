import { PublicConfigurationError } from '../publicConfig';
import { apiFetch } from './transport';

export type Session = { csrf_token: string; expires_at: string };
export type AuthResult =
  | { state: 'signed-in'; session: Session }
  | { state: 'signed-out'; message: string }
  | { state: 'error'; message: string };

const freshLink = 'Open a fresh private login link from your Telegram bot.';

export function bootstrapAuth(): Promise<AuthResult> {
  // Capture and remove bearer material before any request. The promise is created once
  // outside React, so StrictMode effects cannot exchange the same token twice.
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get('token');
  if (window.location.hash || window.location.search) {
    window.history.replaceState(null, '', window.location.pathname);
  }
  return (async () => {
    try {
      if (token) {
        const exchange = await apiFetch('/api/auth/exchange', {
          method: 'POST', cache: 'no-store',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token }),
        });
        if (exchange.status >= 500 || exchange.status === 429) {
          return { state: 'error', message: 'We could not open your workspace. Try again, or request a fresh /profile link in Telegram.' };
        }
        if (!exchange.ok) {
          return { state: 'signed-out', message: `This login link is invalid or expired, or has already been used. ${freshLink}` };
        }
      }
      const response = await apiFetch('/api/session', { cache: 'no-store' });
      if (response.status === 401) return { state: 'signed-out', message: token
        ? 'Your link was accepted, but the browser could not keep your session. It may block or not support this private cookie, or the session may have expired. Update your browser, then request a fresh /profile link in Telegram.'
        : freshLink };
      if (!response.ok) return { state: 'error', message: 'The service is unavailable. Please try again.' };
      return { state: 'signed-in', session: await response.json() as Session };
    } catch (error) {
      if (error instanceof PublicConfigurationError) return { state: 'error', message: `Dashboard configuration needs attention. ${error.message}` };
      return { state: 'error', message: 'Connection failed. Reload, or open a fresh login link.' };
    }
  })();
}
