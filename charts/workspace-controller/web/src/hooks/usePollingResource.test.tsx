import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { SLOW_RECHECK_MS, usePollingResource, type PollingOptions } from './usePollingResource';
import { ResourceStatus } from '../components/ResourceStatus';

type Reading = { value?: string; metricsError?: string };

function Panel({ id = 'a', load, options }: { id?: string; load: (signal: AbortSignal) => Promise<Reading>; options?: PollingOptions }) {
  const resource = usePollingResource(id, load, 1000, options);
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

  it('retries a cold start quietly before showing an error', async () => {
    let calls = 0;
    const load = async () => { calls += 1; if (calls < 3) throw new Error('cold'); return { value: 'warm' }; };
    render(<Panel load={load} options={{ coldRetries: 3, coldRetryMs: 10 }} />);
    expect(screen.getByText('Loading metrics…')).toBeInTheDocument();
    await screen.findByText('warm');
    expect(calls).toBe(3);
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('shows the error once the cold-start retries are used up', async () => {
    const load = vi.fn(async () => { throw new Error('down'); });
    render(<Panel load={load} options={{ coldRetries: 2, coldRetryMs: 10 }} />);
    await screen.findByRole('button', { name: 'Retry' });
    expect(load).toHaveBeenCalledTimes(3);
  });

  it('keeps re-checking a setup state slowly and recovers when it clears', async () => {
    vi.useFakeTimers();
    let reading: Reading = { metricsError: 'metrics_disabled' };
    const load = vi.fn(async () => reading);
    render(<Panel load={load} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Monitoring is not configured.')).toBeInTheDocument();
    // Not on the normal interval...
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(load).toHaveBeenCalledTimes(1);
    // ...but it does come back, and the panel recovers by itself.
    reading = { value: 'reporting now' };
    await act(async () => { await vi.advanceTimersByTimeAsync(SLOW_RECHECK_MS - 10000); });
    expect(load).toHaveBeenCalledTimes(2);
    expect(screen.getByText('reporting now')).toBeInTheDocument();
    expect(screen.queryByText('Monitoring is not configured.')).not.toBeInTheDocument();
  });

  it('shows fresh partial data with its problem, not an older complete reading', async () => {
    let reading: Reading = { value: 'complete, older' };
    const load = async () => reading;
    render(<Panel load={load} />);
    await screen.findByText('complete, older');
    reading = { value: 'partial, newer', metricsError: 'metrics_unavailable' };
    fireEvent(document, new Event('visibilitychange'));
    await screen.findByText('partial, newer');
    expect(screen.getByText('Monitoring is temporarily unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/Showing previous data/)).not.toBeInTheDocument();
  });
});
