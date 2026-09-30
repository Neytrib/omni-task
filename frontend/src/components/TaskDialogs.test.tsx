import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { Task } from '../tasks';
import { NewTaskDialog, TaskDetailsDialog } from './TaskDialogs';

const task: Task = {
  id: 'task-1', owner_id: 'owner-1', title: 'Plan the next step', content: '  The complete message.\nIncluding its ending.  ',
  status: 'pending', source: 'telegram_voice', created_at: '2026-09-30T10:00:00Z',
  updated_at: '2026-09-30T11:00:00Z', version: 1,
};

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');
beforeAll(() => {
  // jsdom cannot model the browser's top layer/inertness. Arc checks cover that.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value(this: HTMLDialogElement) { this.setAttribute('open', ''); } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value(this: HTMLDialogElement) { this.removeAttribute('open'); } });
});
afterAll(() => {
  if (originalShowModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal');
  if (originalClose) Object.defineProperty(HTMLDialogElement.prototype, 'close', originalClose);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, 'close');
});

function newTask(overrides: Partial<Parameters<typeof NewTaskDialog>[0]> = {}) {
  const props = { open: true, onClose: vi.fn(), onCreate: vi.fn(async () => task), disabled: false, ...overrides };
  const view = render(<NewTaskDialog {...props} />);
  return { ...view, props };
}

function details(overrides: Partial<Parameters<typeof TaskDetailsDialog>[0]> = {}) {
  const props = {
    open: true, task, onClose: vi.fn(), onStatusChange: vi.fn(async () => true),
    onDelete: vi.fn(async () => true), pending: false, disabled: false, ...overrides,
  };
  const view = render(<TaskDetailsDialog {...props} />);
  return { ...view, props };
}

describe('new task dialog', () => {
  it('uses a labeled modal, focuses the input, and restores focus under StrictMode', () => {
    const opener = document.createElement('button');
    opener.textContent = 'Open task';
    document.body.append(opener);
    opener.focus();
    const props = { open: true, onClose: vi.fn(), onCreate: vi.fn(async () => task), disabled: false };
    const view = render(<StrictMode><NewTaskDialog {...props} /></StrictMode>);
    expect(screen.getByRole('dialog', { name: 'New task' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'What needs doing?' })).toHaveFocus();
    view.rerender(<StrictMode><NewTaskDialog {...props} open={false} /></StrictMode>);
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it('rejects blank and oversized input without truncating or submitting it', async () => {
    const { props } = newTask();
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: ' \n\t ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Write something');
    const tooLong = 'a'.repeat(50_001);
    fireEvent.change(input, { target: { value: tooLong } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Nothing has been submitted or shortened');
    expect(input).toHaveValue(tooLong);
    expect(props.onCreate).not.toHaveBeenCalled();
  });

  it('counts Unicode code points and submits all accepted content unchanged', async () => {
    const { props } = newTask();
    const content = ` \n${'🎈'.repeat(49_995)}\n  `;
    expect(Array.from(content)).toHaveLength(50_000);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: content } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    await waitFor(() => expect(props.onClose).toHaveBeenCalledOnce());
    expect(props.onCreate).toHaveBeenCalledWith(content, expect.stringMatching(/^[0-9a-f-]{36}$/));
  });

  it('rejects unsupported null characters without attempting a request', () => {
    const { props } = newTask();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep\u0000this' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    expect(screen.getByRole('alert')).toHaveTextContent('unsupported character');
    expect(props.onCreate).not.toHaveBeenCalled();
  });

  it('submits only once under repeated synchronous submissions and blocks cancellation in flight', async () => {
    let finish!: (task: Task) => void;
    const create = vi.fn(() => new Promise<Task>(resolve => { finish = resolve; }));
    const { props } = newTask({ onCreate: create });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    const form = screen.getByRole('textbox').closest('form')!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(create).toHaveBeenCalledOnce();
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
    await act(async () => finish(task));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it.each(['null', 'throw'])('keeps the content and same identity for an uncertain retry (%s)', async outcome => {
    const create = vi.fn().mockImplementationOnce(async () => {
      if (outcome === 'throw') throw new Error('Network unavailable');
      return null;
    }).mockResolvedValueOnce(task);
    newTask({ onCreate: create });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Saving was not confirmed');
    expect(screen.getByRole('textbox')).toHaveValue(task.content);
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly');
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1]).toEqual(create.mock.calls[0]);
  });

  it('retains the pending identity and draft after closing and reopening', async () => {
    const create = vi.fn().mockResolvedValue(null);
    const view = newTask({ onCreate: create });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    await screen.findByRole('alert');
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(view.props.onClose).toHaveBeenCalledOnce();
    view.rerender(<NewTaskDialog {...view.props} open={false} />);
    view.rerender(<NewTaskDialog {...view.props} open />);
    expect(screen.getByRole('textbox')).toHaveValue(task.content);
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1]).toEqual(create.mock.calls[0]);
  });

  it('requires explicit draft abandonment and uses a fresh identity afterward', async () => {
    const create = vi.fn().mockResolvedValue(null);
    const view = newTask({ onCreate: create });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    await screen.findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }));
    expect(screen.getByRole('dialog', { name: 'Discard this draft?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Keep draft' })).toHaveFocus();
    expect(screen.getByText(/may already have saved/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Keep draft' }));
    expect(screen.getByRole('textbox')).toHaveValue(task.content);
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }));
    expect(view.props.onClose).toHaveBeenCalledOnce();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[1][1]).not.toEqual(create.mock.calls[0][1]);
  });

  it('keeps a draft while disconnected and prevents a mutation', () => {
    const { props } = newTask({ disabled: true });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: task.content } });
    fireEvent.submit(screen.getByRole('textbox').closest('form')!);
    expect(props.onCreate).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox')).toHaveValue(task.content);
    expect(screen.getByRole('status')).toHaveTextContent('Reconnect');
  });
});

describe('task details dialog', () => {
  it('renders the entire content as safe text, including markup and its ending', () => {
    const content = `  <img src=x onerror=alert(1)>\n${'long '.repeat(5_000)}THE END  `;
    details({ task: { ...task, content } });
    expect(screen.getByRole('dialog', { name: task.title })).toBeInTheDocument();
    const section = screen.getByRole('region', { name: 'Complete task content' });
    expect(section.querySelector('.task-full-text')?.textContent).toBe(content);
    expect(section.querySelector('img')).toBeNull();
    expect(screen.getByText('Voice transcript')).toBeInTheDocument();
    expect(document.querySelectorAll('time')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Close task details' })).toHaveFocus();
  });

  it('provides a keyboard status selector and treats the current status as a no-op', async () => {
    const { props } = details();
    const select = screen.getByRole('combobox', { name: 'Status' });
    expect(within(select).getAllByRole('option').map(option => option.textContent)).toEqual(['Pending', 'In Progress', 'Completed']);
    fireEvent.change(select, { target: { value: 'pending' } });
    expect(props.onStatusChange).not.toHaveBeenCalled();
    fireEvent.change(select, { target: { value: 'in_progress' } });
    await waitFor(() => expect(props.onStatusChange).toHaveBeenCalledWith(task.id, 'in_progress'));
  });

  it('reports a failed change and renders the authoritative unchanged status', async () => {
    details({ onStatusChange: vi.fn(async () => false) });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'completed' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('status change was not confirmed');
    expect(screen.getByRole('combobox')).toHaveValue('pending');
  });

  it('asks for confirmation, focuses Cancel, and returns to details without deleting', async () => {
    const { props } = details();
    fireEvent.click(screen.getByRole('button', { name: 'Delete task' }));
    expect(screen.getByRole('dialog', { name: 'Delete this task?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    expect(props.onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('dialog', { name: task.title })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete task' })).toHaveFocus());
    expect(props.onDelete).not.toHaveBeenCalled();
  });

  it('Escape cancels confirmation before closing task details', () => {
    const { props } = details();
    fireEvent.click(screen.getByRole('button', { name: 'Delete task' }));
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: task.title })).toBeInTheDocument();
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it('only confirmed deletion calls the API once and closes after success', async () => {
    let finish!: (value: boolean) => void;
    const remove = vi.fn(() => new Promise<boolean>(resolve => { finish = resolve; }));
    const { props } = details({ onDelete: remove });
    fireEvent.click(screen.getByRole('button', { name: 'Delete task' }));
    const confirm = screen.getByRole('button', { name: 'Delete task' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(remove).toHaveBeenCalledExactlyOnceWith(task.id);
    expect(props.onClose).not.toHaveBeenCalled();
    await act(async () => finish(true));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it('leaves a failed deletion visible and allows retry or cancellation', async () => {
    const { props } = details({ onDelete: vi.fn(async () => false) });
    fireEvent.click(screen.getByRole('button', { name: 'Delete task' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Deletion was not confirmed');
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it('keeps content readable with offline mutations disabled', () => {
    const { props } = details({ disabled: true });
    expect(screen.getByRole('region', { name: 'Complete task content' })).toHaveTextContent('Including its ending.');
    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete task' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Done' })).toBeEnabled();
    expect(props.onDelete).not.toHaveBeenCalled();
  });

  it('shows clear loading and disappeared-task states', () => {
    const view = details({ task: null, loading: true });
    expect(screen.getByRole('status')).toHaveTextContent('Loading task details');
    view.rerender(<TaskDetailsDialog {...view.props} loading={false} />);
    expect(screen.getByRole('status')).toHaveTextContent('no longer available');
  });
});
