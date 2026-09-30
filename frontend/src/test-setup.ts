import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

import { TestWebSocket } from './testWebSocket';

beforeEach(() => {
  TestWebSocket.instances = [];
  TestWebSocket.autoReady = true;
  vi.stubGlobal('WebSocket', TestWebSocket);
});

// DOM tests cannot measure browser layout. Arc separately verifies actual drag,
// modal focus containment, and viewport behavior; these are interface shims only.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({ matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent() { return true; },
  });
}
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); localStorage.clear(); sessionStorage.clear(); delete document.documentElement.dataset.theme; });
