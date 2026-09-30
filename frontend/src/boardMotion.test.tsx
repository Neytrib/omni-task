import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useBoardMotion } from './boardMotion';

type Item = { id: string; status: string };
type Played = {
  element: HTMLElement; frames: Keyframe[]; options: KeyframeAnimationOptions;
  progress: number; animation: Animation; cancel: ReturnType<typeof vi.fn>;
};
let board: ReturnType<typeof useBoardMotion>;
let played: Played[];
let reduced: boolean;
let preference: EventTarget;
let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;

function Fixture({ items, dragged = '' }: { items: Item[]; dragged?: string }) {
  board = useBoardMotion(items);
  return <div ref={board.ref}>{['pending', 'in_progress', 'completed'].map((status, column) =>
    <section key={status}>{items.filter(item => item.status === status).map((item, row) =>
      <article key={item.id} data-task-id={item.id} data-x={column * 400} data-y={row * 120}
        data-dnd-dragging={dragged === item.id ? 'true' : undefined}>{item.id}</article>,
    )}</section>,
  )}</div>;
}
const item = (id: string, status = 'pending'): Item => ({ id, status });
function tick() {
  act(() => {
    const pending = [...frames.values()]; frames.clear();
    for (const callback of pending) callback(0);
  });
}

beforeEach(() => {
  played = []; reduced = false; preference = new EventTarget(); frames = new Map(); nextFrame = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame; });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
  vi.stubGlobal('matchMedia', () => ({
    get matches() { return reduced; },
    addEventListener: preference.addEventListener.bind(preference),
    removeEventListener: preference.removeEventListener.bind(preference),
  }));
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const x = Number(this.dataset.x ?? 0); const y = Number(this.dataset.y ?? 0);
    return { x, y, left: x, top: y, right: x + 300, bottom: y + 100, width: 300, height: 100, toJSON: () => ({}) };
  });
  // Geometry and animation are mocked deliberately; no browser layout is claimed.
  vi.stubGlobal('Animation', class {});
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, writable: true,
    value: vi.fn(function (this: HTMLElement, keyframes: Keyframe[], options: KeyframeAnimationOptions) {
      const record = { element: this, frames: keyframes, options, progress: 0 } as Played;
      record.cancel = vi.fn(() => record.animation.oncancel?.call(record.animation, {} as AnimationPlaybackEvent));
      record.animation = {
        effect: { getComputedTiming: () => ({ progress: record.progress }) },
        cancel: record.cancel, onfinish: null, oncancel: null,
      } as unknown as Animation;
      played.push(record); return record.animation;
    }),
  });
});
afterEach(() => { vi.restoreAllMocks(); delete (HTMLElement.prototype as Partial<HTMLElement>).animate; });

it('animates across column remounts and shifts siblings without animating initial content', () => {
  const { container, rerender } = render(<Fixture items={[item('a'), item('b')]} />);
  const original = container.querySelector('[data-task-id="a"]');
  expect(played).toHaveLength(0);
  rerender(<Fixture items={[item('a', 'completed'), item('b')]} />);
  expect(container.querySelector('[data-task-id="a"]')).not.toBe(original);
  const moved = played.find(play => play.element.dataset.taskId === 'a')!;
  expect(moved.frames).toEqual([{ translate: '-800px 0px' }, { translate: '0px 0px' }]);
  expect(moved.options.duration).toBe(320);
  expect(moved.element).toHaveAttribute('data-board-moving');
  expect(played.find(play => play.element.dataset.taskId === 'b')!.frames[0]).toEqual({ translate: '0px 120px' });
  expect(container.querySelectorAll('[data-task-id]')).toHaveLength(2);
});

it('does not animate an unchanged card or invent an origin for a new task', () => {
  const { rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a'), item('b', 'completed')]} />);
  expect(played).toHaveLength(0);
});

it('continues a rapid move or optimistic rollback from its current visible progress', () => {
  const { rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'in_progress')]} />);
  played[0].progress = 0.5;
  rerender(<Fixture items={[item('a')]} />);
  expect(played[0].cancel).toHaveBeenCalledOnce();
  expect(played[0].element).not.toHaveAttribute('data-board-moving');
  expect(played[1].frames[0]).toEqual({ translate: '200px 0px' });
  expect(played[1].element).toHaveAttribute('data-board-moving');
});

it('clears the moving marker on completion or cancellation without a stale callback clearing its replacement', () => {
  const { rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('b'), item('a')]} />);
  const first = played[0];
  expect(first.element).toHaveAttribute('data-board-moving');
  first.progress = 0.5;
  rerender(<Fixture items={[item('b'), item('c'), item('a')]} />);
  const replacement = played.at(-1)!;
  expect(replacement.element).toBe(first.element);
  act(() => first.animation.onfinish?.call(first.animation, {} as AnimationPlaybackEvent));
  expect(replacement.element).toHaveAttribute('data-board-moving');
  act(() => replacement.animation.onfinish?.call(replacement.animation, {} as AnimationPlaybackEvent));
  expect(replacement.element).not.toHaveAttribute('data-board-moving');
  rerender(<Fixture items={[item('a'), item('b'), item('c')]} />);
  const next = played.filter(play => play.element.dataset.taskId === 'a').at(-1)!;
  expect(next.element).toHaveAttribute('data-board-moving');
  act(() => next.animation.oncancel?.call(next.animation, {} as AnimationPlaybackEvent));
  expect(next.element).not.toHaveAttribute('data-board-moving');
});

it('cancels a moving card before drag and never overwrites an active drag transform', () => {
  const { rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'in_progress')]} />);
  act(() => board.beginDrag('a'));
  expect(played[0].cancel).toHaveBeenCalledOnce();
  expect(played[0].element).not.toHaveAttribute('data-board-moving');
  rerender(<Fixture items={[item('a', 'completed')]} dragged="a" />);
  expect(played).toHaveLength(1);
});

it('settles a successful drop from the pointer location rather than its old column', () => {
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  act(() => board.beginDrag('a'));
  const source = container.querySelector<HTMLElement>('[data-task-id="a"]')!;
  source.dataset.x = '650'; source.dataset.y = '45'; source.setAttribute('data-dnd-dragging', 'true');
  act(() => board.finishDrag('a', source));
  rerender(<Fixture items={[item('a', 'completed')]} />);
  expect(played[0].frames[0]).toEqual({ translate: '-150px 45px' });
  tick();
  expect(played).toHaveLength(1);
});

it('waits for drag-plugin cleanup and then settles a same-column drop', () => {
  const { container } = render(<Fixture items={[item('a')]} />);
  act(() => board.beginDrag('a'));
  const source = container.querySelector<HTMLElement>('[data-task-id="a"]')!;
  source.dataset.x = '90'; source.setAttribute('data-dnd-dragging', 'true');
  act(() => board.finishDrag('a', source));
  source.dataset.x = '0'; source.removeAttribute('data-dnd-dragging');
  tick();
  expect(played[0].frames[0]).toEqual({ translate: '90px 0px' });
});

it('defers a cross-column drop if the new node still belongs to drag feedback during commit', () => {
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  act(() => board.beginDrag('a'));
  const source = container.querySelector<HTMLElement>('[data-task-id="a"]')!;
  source.dataset.x = '700';
  act(() => board.finishDrag('a', source));
  rerender(<Fixture items={[item('a', 'completed')]} dragged="a" />);
  expect(played).toHaveLength(0);
  container.querySelector('[data-task-id="a"]')!.removeAttribute('data-dnd-dragging');
  tick();
  expect(played[0].frames[0]).toEqual({ translate: '-100px 0px' });
});

it('does not retain a canceled drag location for a later status change', () => {
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  act(() => board.beginDrag('a'));
  const source = container.querySelector<HTMLElement>('[data-task-id="a"]')!;
  source.dataset.x = '600';
  act(() => board.finishDrag(null, source));
  source.dataset.x = '0'; tick();
  expect(played).toHaveLength(0);
  rerender(<Fixture items={[item('a', 'completed')]} />);
  expect(played[0].frames[0]).toEqual({ translate: '-800px 0px' });
});

it('honors reduced motion initially and cancels existing motion when the preference changes', () => {
  reduced = true;
  const { rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'in_progress')]} />);
  expect(played).toHaveLength(0);
  act(() => { reduced = false; preference.dispatchEvent(new Event('change')); });
  rerender(<Fixture items={[item('a', 'completed')]} />);
  expect(played).toHaveLength(1);
  act(() => { reduced = true; preference.dispatchEvent(new Event('change')); });
  expect(played[0].cancel).toHaveBeenCalledOnce();
  expect(played[0].element).not.toHaveAttribute('data-board-moving');
  rerender(<Fixture items={[item('a')]} />);
  expect(played).toHaveLength(1);
});

it('does not mistake page scrolling for a task movement', () => {
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  vi.stubGlobal('scrollY', 200);
  container.querySelector<HTMLElement>('[data-task-id="a"]')!.dataset.y = '-200';
  rerender(<Fixture items={[item('a'), item('b', 'completed')]} />);
  expect(played).toHaveLength(0);
});

it('cancels animation and remeasures after a responsive layout resize', () => {
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'in_progress')]} />);
  const card = container.querySelector<HTMLElement>('[data-task-id="a"]')!;
  card.dataset.x = '0'; card.dataset.y = '500';
  act(() => window.dispatchEvent(new Event('resize')));
  expect(played[0].cancel).toHaveBeenCalledOnce();
  expect(played[0].element).not.toHaveAttribute('data-board-moving');
  rerender(<Fixture items={[item('a', 'completed')]} />);
  expect(played[1].frames[0]).toEqual({ translate: '-800px 500px' });
});

it('cleans animations and scheduled drop work on removal and unmount without retaining cloned cards', () => {
  const { container, rerender, unmount } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'completed')]} />);
  rerender(<Fixture items={[]} />);
  expect(played[0].cancel).toHaveBeenCalledOnce();
  expect(played[0].element).not.toHaveAttribute('data-board-moving');
  expect(container.querySelectorAll('[data-task-id]')).toHaveLength(0);
  rerender(<Fixture items={[item('b')]} />);
  act(() => { board.beginDrag('b'); board.finishDrag('b'); });
  expect(frames.size).toBe(1);
  unmount();
  expect(frames.size).toBe(0);
  act(() => preference.dispatchEvent(new Event('change')));
  expect(played).toHaveLength(1);
});

it('removes the moving marker from an animated node when the board unmounts', () => {
  const { rerender, unmount } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'completed')]} />);
  const moving = played[0];
  expect(moving.element).toHaveAttribute('data-board-moving');
  unmount();
  expect(moving.cancel).toHaveBeenCalledOnce();
  expect(moving.element).not.toHaveAttribute('data-board-moving');
});

it('leaves task operations usable when the Web Animations API is unavailable', () => {
  delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  const { container, rerender } = render(<Fixture items={[item('a')]} />);
  rerender(<Fixture items={[item('a', 'completed')]} />);
  expect(container.querySelector('[data-task-id="a"]')?.parentElement).toBe(container.querySelectorAll('section')[2]);
});
