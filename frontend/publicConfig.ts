// These are public build settings only. Never add provider or authentication secrets.
export type PublicEnvironment = {
  VITE_API_ORIGIN?: string;
  VITE_WS_ORIGIN?: string;
  VITE_BASE_PATH?: string;
};

export class PublicConfigurationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PublicConfigurationError';
  }
}

function parseOrigin(value: string | undefined, scheme: 'https:' | 'wss:', name: string): string {
  if (!value?.trim()) return '';
  try {
    const url = new URL(value);
    if (url.protocol !== scheme || url.username || url.password || url.pathname !== '/' ||
        url.search || url.hash || /[\s\\]/.test(value)) throw new Error();
    return url.origin;
  } catch {
    // A mistaken credential-bearing URL must not be reflected in logs or the UI.
    throw new PublicConfigurationError(`${name} must be a ${scheme.slice(0, -1).toUpperCase()} origin without credentials, path, query or fragment.`);
  }
}

export function publicConfig(environment: PublicEnvironment) {
  const basePath = environment.VITE_BASE_PATH || '/';
  if (!/^\/(?:[A-Za-z0-9_-]+\/)*$/.test(basePath)) {
    throw new PublicConfigurationError('VITE_BASE_PATH must be / or an absolute directory path ending in /, such as /omni-task/.');
  }
  const apiOrigin = parseOrigin(environment.VITE_API_ORIGIN, 'https:', 'VITE_API_ORIGIN');
  const configuredWebSocket = parseOrigin(environment.VITE_WS_ORIGIN, 'wss:', 'VITE_WS_ORIGIN');
  if (basePath !== '/' && !apiOrigin) {
    throw new PublicConfigurationError('VITE_API_ORIGIN is required for the hosted dashboard. Set its public API HTTPS origin and rebuild.');
  }
  if (configuredWebSocket && (!apiOrigin || new URL(configuredWebSocket).hostname !== new URL(apiOrigin).hostname)) {
    throw new PublicConfigurationError('VITE_WS_ORIGIN must use the API hostname so the HttpOnly session cookie can authenticate it.');
  }
  const webSocketOrigin = configuredWebSocket || apiOrigin.replace(/^https:/, 'wss:');
  return { basePath, apiOrigin, webSocketOrigin };
}

// Pinned dnd-kit injects these three styles. security.test.ts verifies their bytes.
export const dragStyleSources = [
  "'sha256-P3zrZWkaCzrI/B9T0+WLFRTTY8rvTIfnbvP5TBI9Vgo='",
  "'sha256-E7zJXfkufcgTIQG1Th6iYnoGis+rTfZd044bgiM1kr4='",
  "'sha256-YYLJe/bgx7gKdFc8dp3EM3/S8hpKeZs7JyhfBoZYPuk='",
];

export function contentSecurityPolicy(config: ReturnType<typeof publicConfig>) {
  const connections = ["'self'", config.apiOrigin, config.webSocketOrigin].filter(Boolean).join(' ');
  // GitHub Pages cannot set our response headers. frame-ancestors cannot be
  // enforced by a meta tag; the existing Compose proxy still sends that header.
  return `default-src 'self'; script-src 'self'; style-src 'self' ${dragStyleSources.join(' ')}; connect-src ${connections}; object-src 'none'; base-uri 'none'; form-action 'none'`;
}
