// In-memory DOM-test transport only. Never opens a network connection or browser.
export class TestWebSocket {
  static instances: TestWebSocket[] = [];
  static autoReady = true;
  readonly url: string;
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor(url: string | URL) {
    this.url = String(url);
    TestWebSocket.instances.push(this);
    if (TestWebSocket.autoReady) queueMicrotask(() => {
      if (this.readyState === 3) return;
      this.readyState = 1;
      this.onopen?.(new Event('open'));
      this.receive({ type: 'ready', revision: 0, live: true });
    });
  }
  receive(message: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(message) })); }
  disconnect(code = 1006) { this.readyState = 3; this.onclose?.(new CloseEvent('close', { code })); }
  close() { this.readyState = 3; }
}
