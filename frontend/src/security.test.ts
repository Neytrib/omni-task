import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, it, vi } from 'vitest';
import { contentSecurityPolicy, publicConfig } from '../publicConfig';

it('allows only the installed drag library styles through the production CSP', async () => {
  // This check captures registered CSS; it does not simulate browser layout or dragging.
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  const { DragDropManager, StyleInjector } = await import('@dnd-kit/dom');
  const register = vi.spyOn(StyleInjector.prototype, 'register');
  const manager = new DragDropManager();
  try {
    const policies = [readFileSync('nginx.conf', 'utf8'), contentSecurityPolicy(publicConfig({ VITE_API_ORIGIN: 'https://api.example.test', VITE_BASE_PATH: '/omni-task/' }))];
    const styles = [...new Set(register.mock.calls.map(([css]) => css))];
    // Cursor, feedback, and selection prevention must all work in the built app.
    expect(styles).toHaveLength(3);
    for (const css of styles) {
      const digest = createHash('sha256').update(css).digest('base64');
      for (const policy of policies) expect(policy).toContain(`'sha256-${digest}'`);
    }
    for (const policy of policies) {
      expect(policy).toContain("script-src 'self';");
      expect(policy).not.toContain("'unsafe-inline'");
      expect(policy).not.toContain("'unsafe-eval'");
    }
  } finally {
    manager.registry.destroy();
    register.mockRestore();
  }
});
