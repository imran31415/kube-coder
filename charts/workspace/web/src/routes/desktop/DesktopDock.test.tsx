import { render, screen, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import { DesktopDock, formatHotkey } from './DesktopDock';
import { serverMode } from '../../store/server-mode';
import type { DesktopItem } from '../../api/desktop';

const items: DesktopItem[] = [
  {
    id: 'a',
    label: 'Run tests',
    icon: 'icon:terminal',
    hotkey: 'cmd+shift+t',
    action: { type: 'shell', command: 'make test' },
  },
  {
    id: 'b',
    label: 'Docs',
    icon: '📚',
    action: { type: 'url', url: 'https://example.com', target: 'blank' },
  },
];

function noopHandlers() {
  return {
    onLaunch: vi.fn(),
    onEdit: vi.fn(),
    onDelete: vi.fn(),
    onMove: vi.fn(),
    onNew: vi.fn(),
  };
}

beforeEach(() => {
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
});

describe('DesktopDock', () => {
  it('renders compact launch buttons with label + hotkey in the tooltip', () => {
    const h = noopHandlers();
    render(<DesktopDock items={items} {...h} />);
    const btn = screen.getByRole('button', { name: 'Launch Run tests' });
    fireEvent.click(btn);
    expect(h.onLaunch).toHaveBeenCalledWith(items[0]);
    // Tooltip carries the label and formatted hotkey — no always-on chrome.
    expect(screen.getByText('Run tests')).toBeInTheDocument();
    expect(screen.getByText('⌘⇧T')).toBeInTheDocument();
  });

  it('opens a context menu with edit / move / delete on right-click', () => {
    const h = noopHandlers();
    render(<DesktopDock items={items} {...h} />);
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Launch Docs' }));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: /Edit/ }));
    expect(h.onEdit).toHaveBeenCalledWith(items[1]);
    // Menu closes after an action.
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('disables the impossible move directions', () => {
    const h = noopHandlers();
    render(<DesktopDock items={items} {...h} />);
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Launch Run tests' }));
    expect(screen.getByRole('menuitem', { name: /Move left/ })).toBeDisabled();
    expect(screen.getByRole('menuitem', { name: /Move right/ })).not.toBeDisabled();
  });

  it('shows the add button for mutators', () => {
    const h = noopHandlers();
    render(<DesktopDock items={items} {...h} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add icon' }));
    expect(h.onNew).toHaveBeenCalled();
  });

  it('read-only: still launches but hides add and the context menu', () => {
    serverMode.value = { readOnly: true, authed: false, authMode: 'none' };
    const h = noopHandlers();
    render(<DesktopDock items={items} {...h} />);
    expect(screen.getByRole('button', { name: 'Launch Docs' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add icon' })).toBeNull();
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Launch Docs' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('hides entirely for read-only visitors with an empty dock', () => {
    serverMode.value = { readOnly: true, authed: false, authMode: 'none' };
    const h = noopHandlers();
    const { container } = render(<DesktopDock items={[]} {...h} />);
    expect(container.querySelector('.dt-dock')).toBeNull();
  });
});

describe('formatHotkey', () => {
  it('renders modifier glyphs', () => {
    expect(formatHotkey('cmd+shift+t')).toBe('⌘⇧T');
    expect(formatHotkey('ctrl+alt+x')).toBe('⌃⌥X');
  });
});
