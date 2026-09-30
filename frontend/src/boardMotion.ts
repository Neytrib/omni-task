import { useCallback, useLayoutEffect, useRef } from 'react';

type Point = { x: number; y: number };
type Position = Point & { element: HTMLElement };
type Motion = { animation: Animation; element: HTMLElement; from: Point; to: Point };

const duration = 320;
const easing = 'cubic-bezier(0.2, 0.8, 0.2, 1)';
const dragSelector = '[data-dnd-dragging], [data-dnd-dropping], [data-dnd-placeholder]';

function position(element: Element): Point | null {
  const rect = element.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  // Document coordinates do not turn ordinary page scrolling into a card move.
  return { x: rect.left + window.scrollX, y: rect.top + window.scrollY };
}

function currentPosition(motion: Motion): Point {
  const progress = motion.animation.effect?.getComputedTiming().progress;
  const fraction = typeof progress === 'number' ? progress : 1;
  return {
    x: motion.from.x + (motion.to.x - motion.from.x) * fraction,
    y: motion.from.y + (motion.to.y - motion.from.y) * fraction,
  };
}

/** Board-only visual motion. It never delays a mutation or retains task content.
 * Keep dnd-kit Feedback.dropAnimation null: that animation targets its old
 * placeholder, whereas these cards remount in the destination status column.
 */
export function useBoardMotion(tasks: readonly { id: string; status: string }[]) {
  const ref = useRef<HTMLDivElement>(null);
  const previous = useRef(new Map<string, Position>());
  const motions = useRef(new Map<string, Motion>());
  const activeDrag = useRef<string | null>(null);
  const dropped = useRef<{ id: string; point: Point } | null>(null);
  const reduced = useRef(false);
  const frame = useRef<number | null>(null);
  const layout = JSON.stringify(tasks.map(task => [task.id, task.status]));

  const cancelMotions = useCallback(() => {
    for (const [id, { animation, element }] of motions.current) {
      motions.current.delete(id);
      element.removeAttribute('data-board-moving');
      animation.cancel();
    }
  }, []);

  const readPositions = useCallback(() => {
    const result = new Map<string, Position>();
    for (const element of ref.current?.querySelectorAll<HTMLElement>('[data-task-id]') ?? []) {
      const id = element.dataset.taskId;
      if (!id || id === activeDrag.current || element.matches(dragSelector)) continue;
      const point = position(element);
      if (point) result.set(id, { ...point, element });
    }
    return result;
  }, []);

  const animateLayout = useCallback(() => {
    if (!ref.current) return;
    const origins = new Map<string, Point>(previous.current);
    // Carry the visible progress forward even when React removed the old node
    // during another column move or an optimistic rollback.
    for (const [id, motion] of motions.current) origins.set(id, currentPosition(motion));
    cancelMotions();
    const next = readPositions();
    const drop = dropped.current;
    if (drop && next.has(drop.id)) {
      origins.set(drop.id, drop.point);
      dropped.current = null;
    }
    for (const [id, destination] of next) {
      const origin = origins.get(id);
      if (!origin || reduced.current || typeof destination.element.animate !== 'function') continue;
      const x = origin.x - destination.x;
      const y = origin.y - destination.y;
      if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) continue;
      const animation = destination.element.animate(
        [{ translate: `${x}px ${y}px` }, { translate: '0px 0px' }],
        { duration, easing },
      );
      const motion = { animation, element: destination.element, from: origin, to: destination };
      motions.current.set(id, motion);
      destination.element.setAttribute('data-board-moving', '');
      // No fill forwards: completion leaves CSS and dnd-kit fully in charge.
      const finished = () => {
        if (motions.current.get(id) !== motion) return;
        motions.current.delete(id);
        motion.element.removeAttribute('data-board-moving');
      };
      animation.onfinish = finished;
      animation.oncancel = finished;
    }
    // Retain the origin only while the drag plugin temporarily owns that card.
    const active = activeDrag.current && previous.current.get(activeDrag.current);
    if (active && activeDrag.current) next.set(activeDrag.current, active);
    previous.current = next;
  }, [cancelMotions, readPositions]);

  const beginDrag = useCallback((id: string) => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    dropped.current = null;
    cancelMotions();
    previous.current = readPositions();
    activeDrag.current = id;
  }, [cancelMotions, readPositions]);

  // Call before changing task status in onDragEnd, while the source still has
  // dnd-kit's pointer translation. Pass null for a canceled drag.
  const finishDrag = useCallback((id: string | null, element?: Element | null) => {
    const source = element ?? (id ? previous.current.get(id)?.element : null);
    const point = source?.isConnected ? position(source) : null;
    dropped.current = id && point ? { id, point } : null;
    activeDrag.current = null;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      // Also settles same-column drops once the drag plugin removes its styles.
      if (dropped.current) animateLayout();
      dropped.current = null;
    });
  }, [animateLayout]);

  useLayoutEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    reduced.current = query.matches;
    const reset = () => {
      cancelMotions();
      previous.current = readPositions();
      dropped.current = null;
    };
    const preferenceChanged = () => { reduced.current = query.matches; reset(); };
    query.addEventListener('change', preferenceChanged);
    window.addEventListener('resize', reset);
    return () => {
      query.removeEventListener('change', preferenceChanged);
      window.removeEventListener('resize', reset);
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      frame.current = null;
      cancelMotions();
      previous.current.clear();
      dropped.current = null;
      activeDrag.current = null;
    };
  }, [cancelMotions, readPositions]);

  useLayoutEffect(animateLayout, [layout, animateLayout]);

  return { ref, beginDrag, finishDrag };
}
