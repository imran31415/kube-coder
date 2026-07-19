import { render, screen } from '@testing-library/preact';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { GuidePanel } from './GuidePanel';

const STEPS = [
  { title: 'Step one', body: 'do the first thing' },
  { title: 'Step two', body: 'then the second' },
];
const SCENARIOS = [{ prompt: 'status', outcome: 'replies with workspace status' }];

describe('GuidePanel', () => {
  beforeEach(() => localStorage.clear());

  it('shows only the toggle bar when collapsed', () => {
    render(<GuidePanel title="How it works" intro="hi" steps={STEPS} />);
    expect(screen.getByText('How it works')).toBeInTheDocument();
    // Body content is not rendered until opened.
    expect(screen.queryByText('Step one')).toBeNull();
  });

  it('reveals intro, steps and scenarios when opened', async () => {
    const user = userEvent.setup();
    render(
      <GuidePanel title="How it works" intro="the intro" steps={STEPS} scenarios={SCENARIOS} />,
    );
    await user.click(screen.getByText('How it works'));
    expect(screen.getByText('the intro')).toBeInTheDocument();
    expect(screen.getByText('Step one')).toBeInTheDocument();
    expect(screen.getByText('Step two')).toBeInTheDocument();
    expect(screen.getByText('status')).toBeInTheDocument();
    expect(screen.getByText('replies with workspace status')).toBeInTheDocument();
  });

  it('persists the open choice under storageKey', async () => {
    const user = userEvent.setup();
    const { unmount } = render(
      <GuidePanel title="Guide" intro="x" steps={STEPS} storageKey="kc.guide.test" />,
    );
    await user.click(screen.getByText('Guide'));
    expect(localStorage.getItem('kc.guide.test')).toBe('1');
    unmount();

    // A fresh mount reads the stored preference and starts open.
    render(<GuidePanel title="Guide" intro="x" steps={STEPS} storageKey="kc.guide.test" />);
    expect(screen.getByText('Step one')).toBeInTheDocument();
  });
});
