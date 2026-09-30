import { useEffect, useId, useRef, useState, type FormEvent, type RefObject } from 'react';
import type { Task, TaskStatus } from '../tasks';

const contentLimit = 50_000;
const statuses: { value: TaskStatus; label: string }[] = [
  { value: 'pending', label: 'Pending' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'completed', label: 'Completed' },
];

function useModal(open: boolean, initialFocus: RefObject<HTMLElement | null>) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!open || !element) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!element.open) element.showModal();
    initialFocus.current?.focus();
    return () => {
      if (element.open) element.close();
      if (previousFocus?.isConnected && previousFocus !== document.body) previousFocus.focus();
      else document.querySelector<HTMLElement>('[data-dialog-return-focus]')?.focus();
    };
  }, [open, initialFocus]);
  return dialog;
}

function CloseIcon() {
  return <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"><path d="m6 6 12 12M18 6 6 18" /></svg>;
}

function humanTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Time unavailable' : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date);
}

function sourceLabel(source: Task['source']) {
  switch (source) {
    case 'telegram_text': return 'Telegram message';
    case 'telegram_voice': return 'Voice transcript';
    default: return 'Dashboard';
  }
}

export type NewTaskDialogProps = {
  open: boolean;
  onClose: () => void;
  onCreate: (content: string, key: string) => Promise<Task | null>;
  disabled: boolean;
};

export function NewTaskDialog({ open, onClose, onCreate, disabled }: NewTaskDialogProps) {
  const prefix = useId();
  const input = useRef<HTMLTextAreaElement>(null);
  const discardCancel = useRef<HTMLButtonElement>(null);
  const dialog = useModal(open, input);
  const request = useRef<{ content: string; key: string } | null>(null);
  const submitting = useRef(false);
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const count = Array.from(content).length;

  useEffect(() => {
    if (discarding) discardCancel.current?.focus();
    else if (open) input.current?.focus();
  }, [discarding, open]);

  function reset() {
    request.current = null;
    setContent('');
    setUncertain(false);
    setError(null);
    setDiscarding(false);
  }

  function close() {
    if (submitting.current) return;
    if (discarding) {
      setDiscarding(false);
      input.current?.focus();
      return;
    }
    // Closing preserves a draft and any uncertain request identity for later retry.
    onClose();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || disabled) return;
    if (!content.trim()) {
      setError('Write something for your task first.');
      input.current?.focus();
      return;
    }
    if (count > contentLimit) {
      setError('Keep this task within 50,000 characters. Nothing has been submitted or shortened.');
      input.current?.focus();
      return;
    }
    if (/\u0000|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(content)) {
      setError('This text contains an unsupported character. Remove it before saving.');
      input.current?.focus();
      return;
    }
    if (!request.current || request.current.content !== content) {
      request.current = { content, key: crypto.randomUUID() };
    }
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await onCreate(request.current.content, request.current.key);
      if (result) {
        reset();
        onClose();
      } else {
        setUncertain(true);
        setError('Saving was not confirmed. Your text is kept here. Retry the same request safely.');
      }
    } catch {
      setUncertain(true);
      setError('Saving was not confirmed. Your text is kept here. Retry the same request safely.');
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return <dialog ref={dialog} className="task-dialog create-dialog" aria-labelledby={`${prefix}-title`}
    onCancel={event => { event.preventDefault(); close(); }}>
    <div className="dialog-header">
      <div><p className="eyebrow">A little less to remember</p><h2 id={`${prefix}-title`}>{discarding ? 'Discard this draft?' : 'New task'}</h2></div>
      <button type="button" className="icon-button" aria-label="Close new task" disabled={busy} onClick={close}><CloseIcon /></button>
    </div>
    {discarding ? <div className="dialog-body">
      <p>The request may already have saved your task. Refresh the board before creating it again. Discarding only clears this draft.</p>
      <div className="dialog-actions">
        <button ref={discardCancel} type="button" className="button secondary" onClick={() => { setDiscarding(false); input.current?.focus(); }}>Keep draft</button>
        <button type="button" className="button danger" onClick={() => { reset(); onClose(); }}>Discard draft</button>
      </div>
    </div> : <form className="dialog-body" onSubmit={submit} noValidate aria-busy={busy}>
      <label className="field-label" htmlFor={`${prefix}-content`}>What needs doing?</label>
      <textarea ref={input} id={`${prefix}-content`} className="task-input" rows={7} value={content}
        placeholder="Write a thought, a next step, or the whole plan…" readOnly={busy || uncertain}
        aria-invalid={Boolean(error)} aria-describedby={`${prefix}-hint ${error ? `${prefix}-error` : ''}`}
        onChange={event => { setContent(event.target.value); request.current = null; setError(null); }} />
      <div className="field-meta"><p id={`${prefix}-hint`}>Saved in full. Starts in Pending.</p><span className={count > contentLimit ? 'over-limit' : ''}>{count.toLocaleString()} / 50,000</span></div>
      {uncertain && <p className="field-hint">Text is locked until the retry is resolved. You can close this window and return to the same draft.</p>}
      {error && <p id={`${prefix}-error`} className="inline-error" role="alert">{error}</p>}
      {disabled && <p className="field-hint" role="status">Reconnect before saving. Your draft stays here.</p>}
      <div className="dialog-actions">
        {uncertain && <button type="button" className="text-button danger-text" disabled={busy} onClick={() => setDiscarding(true)}>Discard draft</button>}
        <button type="button" className="button secondary" disabled={busy} onClick={close}>{uncertain ? 'Close' : 'Cancel'}</button>
        <button type="submit" className="button primary" disabled={busy || disabled}>{busy ? 'Saving…' : uncertain ? 'Retry save' : 'Create task'}</button>
      </div>
    </form>}
  </dialog>;
}

export type TaskDetailsDialogProps = {
  task: Task | null;
  open: boolean;
  onClose: () => void;
  onStatusChange: (id: string, status: TaskStatus) => Promise<boolean>;
  onDelete: (id: string) => Promise<boolean>;
  pending: boolean;
  disabled: boolean;
  loading?: boolean;
};

export function TaskDetailsDialog({ task, open, onClose, onStatusChange, onDelete, pending, disabled, loading = false }: TaskDetailsDialogProps) {
  const prefix = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const dialog = useModal(open, closeButton);
  const activeRequest = useRef(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setConfirming(false); setError(null); }, [task?.id, open]);
  useEffect(() => { if (confirming) cancelButton.current?.focus(); }, [confirming]);

  function cancelDelete() {
    if (activeRequest.current) return;
    setConfirming(false);
    setError(null);
    requestAnimationFrame(() => deleteButton.current?.focus());
  }

  function close() {
    if (activeRequest.current) return;
    if (confirming) cancelDelete();
    else onClose();
  }

  async function changeStatus(status: TaskStatus) {
    if (!task || status === task.status || activeRequest.current || pending || disabled || loading) return;
    activeRequest.current = true;
    setBusy(true);
    setError(null);
    try {
      if (!await onStatusChange(task.id, status)) setError('The status change was not confirmed. Review the current status and try again.');
    } catch {
      setError('The status change was not confirmed. Review the current status and try again.');
    } finally {
      activeRequest.current = false;
      setBusy(false);
    }
  }

  async function remove() {
    if (!task || !confirming || activeRequest.current || pending || disabled || loading) return;
    activeRequest.current = true;
    setBusy(true);
    setError(null);
    try {
      if (await onDelete(task.id)) { setConfirming(false); onClose(); }
      else setError('Deletion was not confirmed. Your task stays visible until the service confirms it.');
    } catch {
      setError('Deletion was not confirmed. Your task stays visible until the service confirms it.');
    } finally {
      activeRequest.current = false;
      setBusy(false);
    }
  }

  const blocked = busy || pending || disabled || loading;
  return <dialog ref={dialog} className="task-dialog details-dialog" aria-labelledby={`${prefix}-title`}
    aria-busy={busy || loading} onCancel={event => { event.preventDefault(); close(); }}>
    <div className="dialog-header">
      <div><p className="eyebrow">{confirming ? 'One last check' : 'Task details'}</p><h2 id={`${prefix}-title`}>{confirming ? 'Delete this task?' : task?.title ?? 'Task details'}</h2></div>
      <button ref={closeButton} type="button" className="icon-button" aria-label="Close task details" disabled={busy} onClick={close}><CloseIcon /></button>
    </div>
    <div className="dialog-body">
      {confirming && task ? <>
        <p className="delete-explanation">“{task.title}” will be removed from your board and Telegram task list. This cannot be undone.</p>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {disabled && <p className="field-hint">Reconnect before deleting.</p>}
        <div className="dialog-actions">
          <button ref={cancelButton} type="button" className="button secondary" disabled={busy} onClick={cancelDelete}>Cancel</button>
          <button type="button" className="button danger" disabled={blocked} onClick={() => void remove()}>{busy ? 'Deleting…' : 'Delete task'}</button>
        </div>
      </> : task ? <>
        {loading && <p className="field-hint" role="status">Checking the latest task details…</p>}
        <div className="detail-status-field">
          <label className="field-label" htmlFor={`${prefix}-status`}>Status</label>
          <select id={`${prefix}-status`} value={task.status} disabled={blocked} onChange={event => void changeStatus(event.target.value as TaskStatus)}>
            {statuses.map(status => <option key={status.value} value={status.value}>{status.label}</option>)}
          </select>
          {(busy || pending) && <span className="field-hint" role="status">Saving…</span>}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <dl className="task-metadata">
          <div><dt>Source</dt><dd>{sourceLabel(task.source)}</dd></div>
          <div><dt>Created</dt><dd><time dateTime={task.created_at}>{humanTime(task.created_at)}</time></dd></div>
          <div><dt>Updated</dt><dd><time dateTime={task.updated_at}>{humanTime(task.updated_at)}</time></dd></div>
        </dl>
        <section className="full-content" aria-label="Complete task content"><h3>Full content</h3><p className="task-full-text">{task.content}</p></section>
        {disabled && <p className="field-hint">Your last saved content is available. Reconnect to make changes.</p>}
        <div className="dialog-actions details-actions">
          <button ref={deleteButton} type="button" className="text-button danger-text" disabled={blocked} onClick={() => { setError(null); setConfirming(true); }}>Delete task</button>
          <button type="button" className="button secondary" disabled={busy} onClick={close}>Done</button>
        </div>
      </> : <p role="status">{loading ? 'Loading task details…' : 'This task is no longer available. Refresh the board to see your current tasks.'}</p>}
    </div>
  </dialog>;
}
