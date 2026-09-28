import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/preact';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';

// Controllable upload: each test installs its own impl so it can hold the
// promise open (the mobile slow-uplink window) and settle it on cue.
let uploadImpl: (taskId: string, file: File) => Promise<string>;
vi.mock('../tasks/imageAttach', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../tasks/imageAttach')>()),
  uploadTaskFile: vi.fn((taskId: string, file: File) => uploadImpl(taskId, file)),
}));

import { Chat } from './Chat';
import {
  events,
  activeThreadId,
  activeStatus,
  selectedAssistant,
  threads,
  config,
  sending,
  chatError,
} from '../../store/hypervisor';
import { claudeReady } from '../../store/claude';

/**
 * Send racing an in-flight attachment upload (#755).
 *
 * A phone photo over cellular uploads for several seconds — exactly the window
 * in which the user taps Send. The old submit path injected only 'ready'
 * attachment paths and then cleared every chip, so the picture was silently
 * dropped: the message arrived without it and the chip vanished as if it had
 * sent. Now a Send pressed mid-upload QUEUES and dispatches (photo included)
 * the moment the upload lands; a failed upload cancels the queue with an
 * inline error and the draft + chips intact.
 */

const PATH = '/home/dev/.claude-tasks/hypervisor/attachments/pasted-1.jpg';

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      headers: { get: () => 'application/json' },
      json: async () => ({}),
    })),
  );
  config.value = {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/',
    readOnly: false,
    assistants: [{ id: 'claude', label: 'Claude Code', ready: true }] as never,
  };
  activeThreadId.value = null;
  activeStatus.value = 'idle';
  sending.value = false;
  chatError.value = null;
  claudeReady.value = true;
  selectedAssistant.value = 'claude';
  threads.value = [];
  events.value = [];
  URL.createObjectURL = vi.fn(() => 'blob:preview-1');
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  events.value = [];
  threads.value = [];
  activeThreadId.value = null;
  selectedAssistant.value = '';
  config.value = null;
});

function composer() {
  return screen.getByPlaceholderText(/Message Kube-Coder/) as HTMLTextAreaElement;
}

function attachPhoto(container: Element) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['jpeg-bytes'], 'photo.jpg', { type: 'image/jpeg' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  fireEvent.change(input);
}

function submitForm(container: Element) {
  fireEvent.submit(container.querySelector('form.hv-composer')!);
}

/** The user turns pushed (optimistically) by sendMessage — i.e. what was sent. */
function sentTexts(): string[] {
  return events.value.filter((e) => e.role === 'user').map((e) => e.text ?? '');
}

describe('Chat — Send racing an attachment upload (#755)', () => {
  it('queues a Send pressed mid-upload and dispatches with the photo once it lands', async () => {
    let resolveUpload!: (p: string) => void;
    uploadImpl = () => new Promise((res) => { resolveUpload = res; });
    const { container } = render(<Chat />);

    attachPhoto(container);
    fireEvent.input(composer(), { target: { value: 'look at this' } });
    submitForm(container);

    // Nothing sent yet — the send is queued, not dropped, and the button says so.
    expect(sentTexts()).toEqual([]);
    expect(screen.getByRole('button', { name: /Uploading/ })).toBeTruthy();

    resolveUpload(PATH);
    await waitFor(() => expect(sentTexts()).toEqual([`look at this\n${PATH}`]));
  });

  it('sends a photo-only message (no text) queued behind its upload', async () => {
    let resolveUpload!: (p: string) => void;
    uploadImpl = () => new Promise((res) => { resolveUpload = res; });
    const { container } = render(<Chat />);

    attachPhoto(container);
    // With only an uploading photo, Send must be tappable — greyed-out with no
    // explanation was the photo-only mobile experience before.
    const send = screen.getByRole('button', { name: /Send/ }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    submitForm(container);
    expect(sentTexts()).toEqual([]);

    resolveUpload(PATH);
    await waitFor(() => expect(sentTexts()).toEqual([PATH]));
  });

  it('cancels the queued send on upload failure, keeping the draft and saying why', async () => {
    let rejectUpload!: (e: unknown) => void;
    uploadImpl = () => new Promise((_res, rej) => { rejectUpload = rej; });
    const { container } = render(<Chat />);

    attachPhoto(container);
    fireEvent.input(composer(), { target: { value: 'precious draft' } });
    submitForm(container);

    rejectUpload(new Error('boom'));
    await waitFor(() =>
      expect(container.textContent).toContain('message not sent'),
    );
    // Nothing went out, nothing was lost, and the composer is usable again.
    expect(sentTexts()).toEqual([]);
    expect(composer().value).toBe('precious draft');
    expect((screen.getByRole('button', { name: /Send/ }) as HTMLButtonElement)).toBeTruthy();
  });

  it('refuses to send while a failed chip is present instead of dropping it silently', async () => {
    uploadImpl = () => Promise.reject(new Error('boom'));
    const { container } = render(<Chat />);

    attachPhoto(container);
    await waitFor(() =>
      expect(container.textContent).toContain('failed to upload'),
    );
    fireEvent.input(composer(), { target: { value: 'hello' } });
    submitForm(container);

    expect(sentTexts()).toEqual([]);
    expect(composer().value).toBe('hello');
    expect(container.textContent).toContain('remove it to send');
  });

  it('still sends immediately when the upload already finished', async () => {
    uploadImpl = () => Promise.resolve(PATH);
    const { container } = render(<Chat />);

    attachPhoto(container);
    await waitFor(() =>
      expect(container.querySelector('.hv-attachment.is-ready')).not.toBeNull(),
    );
    fireEvent.input(composer(), { target: { value: 'hi' } });
    submitForm(container);

    expect(sentTexts()).toEqual([`hi\n${PATH}`]);
  });
});
