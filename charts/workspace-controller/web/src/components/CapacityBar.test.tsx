import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { CapacityBar } from './CapacityBar';
import { fmtCores } from '../format';
import type { ResourceRollup } from '../api/capacity';

const rollup = (over: Partial<ResourceRollup> = {}): ResourceRollup => ({
  allocatable: 4,
  workspace: 1,
  cluster: 2,
  other: 1,
  workspacePct: 25,
  clusterPct: 50,
  ...over,
});

describe('CapacityBar', () => {
  it('sizes the workspace and other segments by percentage of allocatable', () => {
    const { container } = render(<CapacityBar rollup={rollup()} fmt={fmtCores} />);
    const ws = container.querySelector('.capbar-seg.ws') as HTMLElement;
    const other = container.querySelector('.capbar-seg.other') as HTMLElement;
    expect(ws.style.width).toBe('25%');
    expect(other.style.width).toBe('25%'); // other 1 / alloc 4
  });

  it('shows free headroom (allocatable − used)', () => {
    render(<CapacityBar rollup={rollup()} fmt={fmtCores} />);
    // 4 alloc − (1 ws + 1 other) = 2 cores free
    expect(screen.getByText(/2\.00 cores free of 4\.00 cores/)).toBeInTheDocument();
  });

  it('tones the bar red when cluster utilisation passes the crit threshold', () => {
    const { container } = render(
      <CapacityBar rollup={rollup({ clusterPct: 95 })} fmt={fmtCores} crit={90} />,
    );
    expect(container.querySelector('.capbar.t-crit')).toBeTruthy();
  });

  it('renders an "unknown" track when allocatable is missing', () => {
    const { container } = render(
      <CapacityBar rollup={rollup({ allocatable: null, workspacePct: null, clusterPct: null })} fmt={fmtCores} />,
    );
    expect(container.querySelector('.capbar.unknown')).toBeTruthy();
    expect(screen.getByText(/capacity unknown/)).toBeInTheDocument();
  });

  it('clamps the other segment so scrape skew cannot overflow the track', () => {
    // workspace already at 90%; other would push past 100 — must be capped.
    const { container } = render(
      <CapacityBar rollup={rollup({ allocatable: 4, workspace: 3.6, workspacePct: 90, other: 2, cluster: 5.6, clusterPct: 140 })} fmt={fmtCores} />,
    );
    const other = container.querySelector('.capbar-seg.other') as HTMLElement;
    expect(parseFloat(other.style.width)).toBeLessThanOrEqual(10); // 100 − 90
  });
});
