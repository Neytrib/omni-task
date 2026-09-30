import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DragDropManager, Draggable, PointerSensor } from '@dnd-kit/dom';
import { taskPointerSensor } from './cardDrag';

function pointer(target: EventTarget, type: string, values: Partial<PointerEventInit> = {}) {
  const event = new PointerEvent(type, {
    bubbles: true, cancelable: true, isPrimary: true, pointerId: 1,
    pointerType: 'mouse', button: 0, buttons: type === 'pointerup' ? 0 : 1,
    clientX: 10, clientY: 10, ...values,
  });
  target.dispatchEvent(event);
  return event;
}

function fixture() {
  const card = document.createElement('article');
  card.innerHTML = `
    <span data-part="source">Telegram</span>
    <button data-part="grip"><span data-part="grip-icon">Move</span></button>
    <h3><button data-task-open data-part="title"><span data-part="title-text">A task</span></button></h3>
    <p data-part="preview">The complete card is draggable.</p>
    <label data-no-drag data-part="status-label">Status
      <select data-part="status"><option data-part="option">Pending</option></select>
    </label>
    <button data-part="other-control">Another action</button>`;
  document.body.append(card);
  const part = (name: string) => card.querySelector<HTMLElement>(`[data-part="${name}"]`)!;
  const manager = new DragDropManager({ plugins: [], sensors: [] });
  const source = new Draggable({ id: 'sensor-test-task', element: card, handle: part('grip'), register: false }, manager);
  // Exercise the installed sensor and DOM event binding, stopping at the layout
  // boundary: jsdom is not evidence of collision geometry or native dragging.
  const start = vi.spyOn(manager.actions, 'start').mockImplementation(() => {
    const controller = new AbortController();
    controller.abort();
    return controller;
  });
  const sensor = new PointerSensor(manager, taskPointerSensor.options);
  const unbind = sensor.bind(source);
  return {
    card, part, source, start,
    dispose() {
      unbind(); sensor.destroy(); source.destroy(); manager.registry.destroy();
      start.mockRestore(); card.remove();
    },
  };
}

describe('whole task card pointer activation', () => {
  let view: ReturnType<typeof fixture>;
  beforeEach(() => { vi.useFakeTimers(); view = fixture(); });
  afterEach(() => { view.dispose(); vi.useRealTimers(); });

  it.each(['card', 'preview', 'source', 'title', 'title-text', 'grip', 'grip-icon'])('starts a mouse drag from %s after movement', targetName => {
    const target = targetName === 'card' ? view.card : view.part(targetName);
    pointer(target, 'pointerdown');
    pointer(document, 'pointermove', { clientX: 14 });
    expect(view.start).not.toHaveBeenCalled();
    pointer(document, 'pointermove', { clientX: 19 });
    expect(view.start).toHaveBeenCalledOnce();
    expect(view.start).toHaveBeenCalledWith(expect.objectContaining({ source: view.source }));
  });

  it('uses movement activation for a pen on the card', () => {
    pointer(view.part('preview'), 'pointerdown', { pointerType: 'pen' });
    pointer(document, 'pointermove', { pointerType: 'pen', clientX: 19 });
    expect(view.start).toHaveBeenCalledOnce();
  });

  it('does not activate or prevent an ordinary title click below the movement threshold', () => {
    const title = view.part('title');
    const click = vi.fn();
    title.addEventListener('click', click);
    const down = pointer(title, 'pointerdown');
    pointer(document, 'pointermove', { clientX: 13 });
    pointer(title, 'pointerup', { clientX: 13 });
    title.click();
    vi.advanceTimersByTime(500);
    expect(down.defaultPrevented).toBe(false);
    expect(view.start).not.toHaveBeenCalled();
    expect(click).toHaveBeenCalledOnce();
  });

  it.each(['status-label', 'status', 'option', 'other-control'])('leaves %s interactive without starting a mouse or touch drag', name => {
    const target = view.part(name);
    pointer(target, 'pointerdown');
    pointer(document, 'pointermove', { clientX: 100 });
    pointer(document, 'pointerup');
    pointer(target, 'pointerdown', { pointerType: 'touch' });
    vi.advanceTimersByTime(300);
    expect(view.start).not.toHaveBeenCalled();
  });

  it('starts a touch drag after a deliberate hold on the title', () => {
    pointer(view.part('title'), 'pointerdown', { pointerType: 'touch' });
    vi.advanceTimersByTime(249);
    expect(view.start).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(view.start).toHaveBeenCalledOnce();
  });

  it('cancels touch activation when the user begins scrolling before the hold completes', () => {
    pointer(view.part('preview'), 'pointerdown', { pointerType: 'touch' });
    vi.advanceTimersByTime(100);
    const move = pointer(document, 'pointermove', { pointerType: 'touch', clientY: 20 });
    vi.advanceTimersByTime(300);
    expect(view.start).not.toHaveBeenCalled();
    expect(move.defaultPrevented).toBe(false);
  });

  it('cancels touch activation when the user releases before the hold completes', () => {
    pointer(view.part('preview'), 'pointerdown', { pointerType: 'touch' });
    vi.advanceTimersByTime(100);
    pointer(document, 'pointerup', { pointerType: 'touch' });
    vi.advanceTimersByTime(300);
    expect(view.start).not.toHaveBeenCalled();
  });

  it('does not activate a disabled card', () => {
    view.source.disabled = true;
    pointer(view.card, 'pointerdown');
    pointer(document, 'pointermove', { clientX: 100 });
    vi.advanceTimersByTime(300);
    expect(view.start).not.toHaveBeenCalled();
  });
});
