import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PublishPanel } from './PublishPanel';
import { getPublish, publishAction, type PublishStatus } from '../../api/publish';
import { ApiError } from '../../api/client';

vi.mock('../../api/publish', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/publish')>(),
  getPublish: vi.fn(), publishAction: vi.fn(), getPublishDiff: vi.fn(),
}));
function prepared(): PublishStatus {
  return { supported: true, eligibility: { can_prepare: true, can_publish: true, reason: null },
    preparing: null, preparation_error: null, operation: null, pr: null,
    preparation: { id: 'prep-1', fingerprint: 'fingerprint', draft_revision: 1, title: 'Fix app', body: 'Summary', draft: false,
      summary_status: 'ready', author: { name: 'Developer', email: 'dev@example.test' }, checks: { state: 'unknown' },
      files: [{ path: 'app.ts', added: 1, deleted: 0, binary: false }],
      destination: { head_repo: 'me/app', head_branch: 'kc/change', base_repo: 'team/app', base_branch: 'develop', identity: 'personal:1' } } };
}
beforeEach(() => { vi.mocked(getPublish).mockReset(); vi.mocked(publishAction).mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('Build publication', () => {
  it('hides publishing on an older server', async () => {
    vi.mocked(getPublish).mockRejectedValue(new ApiError('Not found', 404, {}));
    const { container } = render(<PublishPanel taskId="t1" />);
    await waitFor(() => expect(getPublish).toHaveBeenCalled());
    expect(container.textContent).toBe('');
  });

  it('saves edits before publishing and suppresses double taps', async () => {
    const s = prepared();
    vi.mocked(getPublish).mockResolvedValue(s);
    let finish: (v: PublishStatus) => void = () => {};
    vi.mocked(publishAction).mockImplementation(async (_id, action) => {
      if (action === 'draft') return { ...s, preparation: { ...s.preparation!, title: 'My title', draft_revision: 2 } };
      return new Promise(resolve => { finish = resolve; });
    });
    render(<PublishPanel taskId="t1" />);
    await screen.findByLabelText('PR title');
    fireEvent.input(screen.getByLabelText('PR title'), { target: { value: 'My title' } });
    fireEvent.click(screen.getByText('Push & Open PR'));
    fireEvent.click(screen.getByText('Push & Open PR'));
    await waitFor(() => expect(publishAction).toHaveBeenCalledTimes(2));
    expect(vi.mocked(publishAction).mock.calls[0][1]).toBe('draft');
    expect(vi.mocked(publishAction).mock.calls[1][2]).toMatchObject({ preparation_id: 'prep-1', draft_revision: 2, fingerprint: 'fingerprint' });
    finish({ ...s, operation: { id: 'pub-1', stage: 'pushing' } });
    await screen.findByText('Publishing…');
    expect(screen.getByText('Publishing…')).toBeDisabled();
  });

  it('keeps user text when saving loses connection', async () => {
    vi.mocked(getPublish).mockResolvedValue(prepared());
    vi.mocked(publishAction).mockRejectedValue(new Error('Connection lost'));
    render(<PublishPanel taskId="t1" />);
    await screen.findByLabelText('PR title');
    fireEvent.input(screen.getByLabelText('PR title'), { target: { value: 'Keep my edit' } });
    fireEvent.click(screen.getByText('Save description'));
    await screen.findByText('Connection lost');
    expect(screen.getByLabelText('PR title')).toHaveValue('Keep my edit');
  });

  it('does not put an old Build response into a newly selected Build', async () => {
    let old: (v: PublishStatus) => void = () => {};
    vi.mocked(getPublish).mockImplementation(id => id === 'old' ? new Promise(r => { old = r; }) : Promise.resolve(prepared()));
    const { rerender } = render(<PublishPanel taskId="old" />);
    rerender(<PublishPanel taskId="new" />);
    await screen.findByLabelText('PR title');
    const stale = prepared(); stale.preparation!.title = 'Wrong Build';
    old(stale);
    await Promise.resolve();
    expect(screen.getByLabelText('PR title')).toHaveValue('Fix app');
  });

  it('offers updates to the existing PR after a fresh review', async () => {
    const s = prepared();
    s.operation = { id: 'pub-old', preparation_id: 'prep-old', stage: 'published' };
    s.pr = { number: 7, url: 'https://github.com/team/app/pull/7', state: 'open', draft: false, head_sha: 'sha' };
    vi.mocked(getPublish).mockResolvedValue(s);
    render(<PublishPanel taskId="t1" />);
    expect(await screen.findByText('Push updates')).toBeEnabled();
    expect(screen.getByText('Open PR #7 · open')).toHaveAttribute('href', s.pr.url);
    expect(screen.getByText(/Tests: unknown/)).toBeInTheDocument();
  });

  it('replaces the completed publish action with a direct PR link', async () => {
    const s = prepared();
    s.operation = { id: 'pub-done', preparation_id: s.preparation!.id, stage: 'published' };
    s.pr = { number: 7, url: 'https://github.com/team/app/pull/7', state: 'open', draft: false, head_sha: 'sha' };
    vi.mocked(getPublish).mockResolvedValue(s);
    render(<PublishPanel taskId="t1" />);
    expect(await screen.findByRole('link', { name: 'View PR' })).toHaveAttribute('href', s.pr.url);
    expect(screen.queryByText('Push updates')).not.toBeInTheDocument();
  });
});
