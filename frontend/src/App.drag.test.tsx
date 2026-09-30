import type { ReactNode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { App } from './App';
import type { Task } from './tasks';

type DragEvent = { canceled?: boolean; operation: { source: { id: string }; target?: { id: string } } };
const drag = vi.hoisted(() => ({ start: (_event: DragEvent) => {}, end: (_event: DragEvent) => {} }));
// Sensor activation is exercised separately against the installed PointerSensor.
// Here provider events exercise the real board/API wiring and post-drag clicks.
vi.mock('@dnd-kit/react', () => ({
  DragDropProvider: ({ children, onDragStart, onDragEnd }: { children: ReactNode; onDragStart: typeof drag.start; onDragEnd: typeof drag.end }) => {
    drag.start = onDragStart; drag.end = onDragEnd; return children;
  },
  useDraggable: () => ({ ref: undefined, handleRef: undefined, isDragging: false }),
  useDroppable: () => ({ ref: undefined, isDropTarget: false }),
}));

async function board() {
  const session = { csrf_token: 'test-csrf', expires_at: '2030-01-01T00:00:00Z' };
  let task: Task = { id: '10000000-0000-0000-0000-000000000001', owner_id: 'test-owner', title: 'Drag this task', content: 'The full card can move.', source: 'dashboard', status: 'pending', version: 1, created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:00Z' };
  const request = vi.fn(async (path: string, options?: RequestInit) => {
    let result: unknown;
    if (path === '/api/session') result = session;
    else if (options?.method === 'PATCH') { task = { ...task, status: JSON.parse(options.body as string).status, version: task.version + 1 }; result = task; }
    else if (path.startsWith('/api/tasks?')) result = { items: [task], revision: task.version, next_cursor: null };
    else result = task;
    return { ok: true, status: 200, json: async () => result };
  });
  vi.stubGlobal('fetch', request);
  render(<App authentication={Promise.resolve({ state: 'signed-in', session })} />);
  await screen.findByRole('button', { name: task.title });
  return { id: task.id, title: task.title, request };
}

it('persists a whole-card drop without also opening details, then allows the next click', async () => {
  const { id, title, request } = await board();
  act(() => drag.start({ operation: { source: { id } } }));
  act(() => drag.end({ operation: { source: { id }, target: { id: 'completed' } } }));
  await waitFor(() => expect(screen.getByLabelText(`Status for ${title}`)).toHaveValue('completed'));
  fireEvent.click(screen.getByRole('button', { name: title }), { detail: 1 });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(request.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(1);
  fireEvent.pointerDown(screen.getByRole('button', { name: title }), { button: 0, isPrimary: true, pointerType: 'mouse' });
  fireEvent.click(screen.getByRole('button', { name: title }), { detail: 1 });
  expect(await screen.findByRole('dialog', { name: title })).toBeInTheDocument();
});

it('does not save a canceled drag and keeps keyboard title activation available', async () => {
  const { id, title, request } = await board();
  act(() => drag.start({ operation: { source: { id } } }));
  act(() => drag.end({ canceled: true, operation: { source: { id }, target: { id: 'completed' } } }));
  fireEvent.click(screen.getByRole('button', { name: title }), { detail: 1 });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(request.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: title }), { detail: 0 });
  expect(await screen.findByRole('dialog', { name: title })).toBeInTheDocument();
});
