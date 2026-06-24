import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/preact';
import { CapacityChart } from './CapacityChart';
import { fmtCores } from '../format';
import type { Series } from '../api/workspaces';

const series = (vals: number[]): Series => vals.map((v, i) => [1000 + i * 60, v]);

describe('CapacityChart', () => {
  it('shows an empty state with fewer than two points', () => {
    render(<CapacityChart workspace={[]} cluster={series([1])} allocatable={series([4])} fmt={fmtCores} />);
    expect(screen.getByText(/No data in this range/)).toBeInTheDocument();
  });

  it('draws the cluster area, workspace line, and allocatable ceiling', () => {
    const { container } = render(
      <CapacityChart
        workspace={series([0.5, 0.6, 0.7])}
        cluster={series([1, 1.5, 2])}
        allocatable={series([4, 4, 4])}
        fmt={fmtCores}
      />,
    );
    expect(container.querySelector('polygon')).toBeTruthy(); // cluster area
    expect(container.querySelectorAll('polyline').length).toBe(2); // cluster + ws lines
    expect(container.querySelector('line')).toBeTruthy(); // dashed ceiling
  });

  it('labels the peak total usage and the ceiling', () => {
    render(
      <CapacityChart
        workspace={series([0.5, 0.6])}
        cluster={series([1, 3])}
        allocatable={series([4, 4])}
        fmt={fmtCores}
      />,
    );
    expect(screen.getByText(/peak total 3\.00 cores/)).toBeInTheDocument();
    expect(screen.getByText(/ceiling 4\.00 cores/)).toBeInTheDocument();
  });
});
