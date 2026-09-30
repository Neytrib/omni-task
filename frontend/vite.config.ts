import { defineConfig, loadEnv } from 'vite';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { contentSecurityPolicy, publicConfig } from './publicConfig.ts';

export default defineConfig(({ mode, command }) => {
  const environment = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), 'VITE_');
  const config = publicConfig(environment);
  return {
    base: config.basePath,
    // Only this explicit public allowlist enters the client bundle.
    envPrefix: [],
    define: {
      'import.meta.env.VITE_API_ORIGIN': JSON.stringify(config.apiOrigin),
      'import.meta.env.VITE_WS_ORIGIN': JSON.stringify(config.webSocketOrigin),
      'import.meta.env.VITE_BASE_PATH': JSON.stringify(config.basePath),
    },
    plugins: [react(), {
      name: 'static-host-security-policy',
      transformIndexHtml: html => html.replace('<!-- static-security-policy -->', command === 'build'
        ? `<meta http-equiv="Content-Security-Policy" content="${contentSecurityPolicy(config)}" />`
        : ''),
    }],
  };
});
