import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from '../../store/ui';
import { ScanReportPanel } from './ScanReportPanel';

const REPORT = { text: '## Executive summary\n\nNothing proven.', location: '/runs/r1/report.md' };

describe('ScanReportPanel', () => {
  beforeEach(() => {
    toasts.value = [];
  });
  afterEach(() => vi.unstubAllGlobals());

  it('leads with where the report is saved, then the report itself', () => {
    render(<ScanReportPanel report={REPORT} onClose={() => {}} />);
    expect(screen.getByText('Saved in your workspace at')).toBeTruthy();
    expect(screen.getByText('/runs/r1/report.md')).toBeTruthy();
    expect(screen.getByText('Nothing proven.')).toBeTruthy();
  });

  it('says nothing about saving when the backend gave no location', () => {
    render(<ScanReportPanel report={{ ...REPORT, location: '' }} onClose={() => {}} />);
    expect(screen.queryByText('Saved in your workspace at')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Copy path' })).toBeNull();
  });

  it('copies the path and confirms it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(<ScanReportPanel report={REPORT} onClose={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Copy path' }));
    expect(writeText).toHaveBeenCalledWith('/runs/r1/report.md');
    expect(toasts.value.map((t) => t.message)).toEqual(['Path copied']);
  });

  it('says how to get the path by hand when copying is refused', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(<ScanReportPanel report={REPORT} onClose={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Copy path' }));
    expect(toasts.value[0]?.kind).toBe('warn');
    expect(toasts.value[0]?.message).toMatch(/copy it by hand/);
  });

  it('hides when asked', async () => {
    const onClose = vi.fn();
    render(<ScanReportPanel report={REPORT} onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: 'Hide report' }));
    expect(onClose).toHaveBeenCalled();
  });
});
