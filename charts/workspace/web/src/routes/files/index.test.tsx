import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  uploadBatch: vi.fn(),
  uploadZip: vi.fn(),
}));

vi.mock('../../api/files', async (orig) => ({
  ...(await orig<typeof import('../../api/files')>()),
  listFiles: () => Promise.resolve({ path: '', entries: [] }),
  uploadBatch: mocks.uploadBatch,
  uploadZip: mocks.uploadZip,
}));
vi.mock('../../store/metrics', async (orig) => ({
  ...(await orig<typeof import('../../store/metrics')>()),
  refreshMetrics: vi.fn(),
}));

import * as FilesRoute from './index';
import { ApiError } from '../../api/client';
import { metrics } from '../../store/metrics';
import { toasts } from '../../store/ui';
import { serverMode } from '../../store/server-mode';
import type { SystemMetrics } from '../../api/metrics';

const Files = Object.values(FilesRoute)[0] as () => preact.JSX.Element;
const MSG = 'Upload storage is full: raise files.uploadQuota in the chart values.';

function setUsage(used: number, quota: number) {
  metrics.value = {
    uploads: { used_bytes: used, quota_bytes: quota, available_bytes: quota - used,
               percent: quota ? (used / quota) * 100 : 0 },
  } as unknown as SystemMetrics;
}

function pick(container: Element, selector: string, file: File) {
  const input = container.querySelector(selector) as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  fireEvent.change(input);
}

describe('Files upload storage (#556)', () => {
  beforeEach(() => {
    toasts.value = [];
    metrics.value = null;
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
    mocks.uploadBatch.mockReset();
    mocks.uploadZip.mockReset();
  });

  it('shows how full upload storage is', async () => {
    setUsage(1024 * 1024 * 1024, 2 * 1024 * 1024 * 1024);
    render(<Files />);
    await waitFor(() => expect(screen.getByText(/Upload storage: 1\.0 GB of 2\.0 GB used \(50%\)/)).toBeTruthy());
  });

  it('turns the note into an alert near the limit', async () => {
    setUsage(95, 100);
    render(<Files />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Upload storage'));
  });

  it('says nothing when no limit is set or the pod is older', async () => {
    setUsage(5, 0);
    const { container, rerender } = render(<Files />);
    await waitFor(() => expect(container.querySelector('.files-table')).not.toBeNull());
    expect(container.textContent).not.toContain('Upload storage');
    metrics.value = {} as SystemMetrics;
    rerender(<Files />);
    expect(container.textContent).not.toContain('Upload storage');
  });

  it("puts the server's reason in the batch failure toast", async () => {
    mocks.uploadBatch.mockResolvedValue({ done: 0, failed: [{ name: 'a.bin', error: MSG }] });
    const { container } = render(<Files />);
    pick(container, 'input[type=file][multiple]:not([accept])', new File(['x'], 'a.bin'));
    await waitFor(() => expect(toasts.value.length).toBeGreaterThan(0));
    expect(toasts.value[0].kind).toBe('danger');
    expect(toasts.value[0].message).toContain('a.bin');
    expect(toasts.value[0].message).toContain('files.uploadQuota');
  });

  it("shows the server's reason when an unzip is refused", async () => {
    mocks.uploadZip.mockRejectedValue(new ApiError(MSG, 507, { code: 'upload_quota_exceeded' }));
    const { container } = render(<Files />);
    pick(container, 'input[accept=".zip"]', new File(['x'], 'a.zip'));
    await waitFor(() => expect(toasts.value.map((t) => t.message)).toContain(MSG));
  });
});
