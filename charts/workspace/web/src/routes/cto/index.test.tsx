import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { CtoRoute } from './index';
import { newChatMode } from '../../store/hypervisor';

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));
vi.mock('../../store/router', () => ({
  navigate: (...a: unknown[]) => navigate(...a),
  currentPath: { value: '/cto' },
}));

/**
 * `/cto` is a permanent redirect, not a page (#683).
 *
 * The route has to keep resolving: bookmarks, the docs, and anything that ever
 * linked to the AI CTO point here, and 404ing them would be the one way this
 * refactor could be felt as a regression.
 */

beforeEach(() => {
  vi.clearAllMocks();
  newChatMode.value = '';
});

afterEach(() => {
  newChatMode.value = '';
});

describe('/cto redirect', () => {
  it('lands the user in Chat', async () => {
    render(<CtoRoute />);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/hypervisor', true));
  });

  it('pre-selects CTO mode, so the landing is the AI CTO and not a plain chat', async () => {
    render(<CtoRoute />);
    await waitFor(() => expect(newChatMode.value).toBe('cto'));
  });

  it('replaces rather than pushes, so Back does not bounce through it', async () => {
    render(<CtoRoute />);
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    expect(navigate.mock.calls[0][1]).toBe(true);
  });

  it('renders nothing of its own', () => {
    const { container } = render(<CtoRoute />);
    expect(container.textContent).toBe('');
  });
});
