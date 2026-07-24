import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach } from 'vitest';
import { MoreSheet } from './index';
import { currentPath, navigate } from '../../store/router';
import { sheetOpen, theme } from '../../store/ui';
import { serverMode } from '../../store/server-mode';

beforeEach(() => {
  sheetOpen.value = 'more';
  theme.value = 'system';
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  navigate('/desktop', true);
});

describe('MoreSheet categories (#267)', () => {
  it('renders Quick actions plus the three nav category sections', () => {
    render(<MoreSheet />);
    for (const title of ['Quick actions', 'Mission Control', 'Workspace', 'Knowledge']) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    }
    // Quick actions keeps the pre-existing action entries.
    expect(screen.getByText('New terminal')).toBeInTheDocument();
    expect(screen.getByText('Open VS Code')).toBeInTheDocument();
    expect(screen.getByText('Switch to light')).toBeInTheDocument();
    // Display labels from the shared source of truth.
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Builds')).toBeInTheDocument();
    // Settings stays a standalone entry with no category.
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('hides the mutating quick actions in read-only mode', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'basic', demoShowAll: false };
    render(<MoreSheet />);
    expect(screen.queryByText('New terminal')).toBeNull();
    expect(screen.queryByText('Open VS Code')).toBeNull();
    // Theme toggle survives — it's not a mutation.
    expect(screen.getByText('Switch to light')).toBeInTheDocument();
  });

  it('the Mission Control Overview entry navigates to /mission and closes the sheet', () => {
    render(<MoreSheet />);
    screen.getByText('Overview').click();
    expect(currentPath.value).toBe('/mission');
    expect(sheetOpen.value).toBeNull();
  });
});
