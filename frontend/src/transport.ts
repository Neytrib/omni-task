import { publicConfig, PublicConfigurationError } from '../publicConfig';

function configuration() {
  const config = publicConfig({
    VITE_API_ORIGIN: import.meta.env.VITE_API_ORIGIN,
    VITE_WS_ORIGIN: import.meta.env.VITE_WS_ORIGIN,
    VITE_BASE_PATH: import.meta.env.VITE_BASE_PATH,
  });
  if (!config.apiOrigin && window.location.hostname.endsWith('.github.io')) {
    throw new PublicConfigurationError('The hosted dashboard has no API configured. Set VITE_API_ORIGIN and rebuild.');
  }
  return config;
}

export function apiUrl(path: string): string {
  // Callers cannot accidentally send a session or CSRF token to an arbitrary URL.
  if (!path.startsWith('/api/') || /[\\#\s]/.test(path) || new URL(path, 'https://api.invalid').pathname.indexOf('/api/') !== 0) {
    throw new PublicConfigurationError('Invalid API request path.');
  }
  return `${configuration().apiOrigin}${path}`;
}

export function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const url = apiUrl(path);
  const crossOrigin = new URL(url, window.location.origin).origin !== window.location.origin;
  return fetch(url, {
    ...options,
    credentials: crossOrigin ? 'include' : 'same-origin',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
  });
}

export function taskWebSocketUrl(): URL {
  const config = configuration();
  const origin = config.webSocketOrigin || window.location.origin.replace(/^http/, 'ws');
  return new URL('/api/ws/tasks', origin);
}
