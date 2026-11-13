import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { usePollingResource } from './usePollingResource';
import { ResourceStatus } from '../components/ResourceStatus';

function Panel({ id = 'a', load }: { id?: string; load: (signal: AbortSignal) => Promise<{ value: string }> }) {
  const resource = usePollingResource(id, load, 1000);
  return <><span>{resource.data?.value}</span><ResourceStatus {...resource} /></>;
}

afterEach(() => vi.useRealTimers());

describe('metrics recovery', () => {
  it('retains the last reading, marks it stale and recovers through Retry', async () => {
    let fail = false;
    const load = async () => { if (fail) throw new Error('private upstream body'); return { value: '42 cores' }; };
    render(<Panel load={load} />);
    await screen.findByText('42 cores');
    fail = true;
    fireEvent(document, new Event('visibilitychange'));
    await screen.findByText(/Showing previous data/);
    expect(screen.getByText('42 cores')).toBeInTheDocument();
    expect(screen.queryByText(/private upstream/)).not.toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByText(/Showing previous data/)).not.toBeInTheDocument());
  });

  it('aborts a replaced resource and ignores its late result', async () => {
    let oldSignal: AbortSignal | undefined;
    let finish: ((value: { value: string }) => void) | undefined;
    const load = (signal: AbortSignal) => { oldSignal = signal; return new Promise<{ value: string }>(resolve => { finish = resolve; }); };
    const view = render(<Panel load={load} />);
    await waitFor(() => expect(oldSignal).toBeDefined());
    view.rerender(<Panel id="b" load={async () => ({ value: 'new workspace' })} />);
    await screen.findByText('new workspace');
    expect(oldSignal!.aborted).toBe(true);
    await act(async () => finish!({ value: 'old workspace' }));
    expect(screen.queryByText('old workspace')).not.toBeInTheDocument();
  });

  it('does not overlap a pending poll when visibility changes', async () => {
    const load = vi.fn(() => new Promise<{ value: string }>(() => {}));
    const view = render(<Panel load={load} />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    fireEvent(document, new Event('visibilitychange'));
    fireEvent(document, new Event('visibilitychange'));
    expect(load).toHaveBeenCalledTimes(1);
    view.unmount();
  });
});
