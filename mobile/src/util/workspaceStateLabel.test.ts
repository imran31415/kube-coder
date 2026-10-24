/**
 * The Controller screen's words for a workspace's state (#728 step 5): a
 * sleeping workspace reads as resting, not as stopped.
 */
import { describe, expect, it } from 'vitest';
import { stateLabel, stopQuestion } from './workspaceStateLabel';

describe('stateLabel', () => {
  it('names every state the controller sends', () => {
    expect(stateLabel('running')).toEqual({ tone: 'success', label: 'Running' });
    expect(stateLabel('sleeping')).toEqual({ tone: 'info', label: 'Sleeping' });
    expect(stateLabel('stopped')).toEqual({ tone: 'textFaint', label: 'Stopped' });
    expect(stateLabel('transitioning')).toEqual({ tone: 'warning', label: 'Working' });
    expect(stateLabel('degraded')).toEqual({ tone: 'danger', label: 'Degraded' });
  });

  it("shows a newer controller's unknown state as itself, muted, instead of breaking", () => {
    expect(stateLabel('hibernating')).toEqual({ tone: 'textMuted', label: 'hibernating' });
  });
});

describe('stopQuestion', () => {
  it('says what Stop changes for a sleeping workspace: a visit stops waking it', () => {
    const q = stopQuestion({ state: 'sleeping', user: 'gus' });
    expect(q).toContain('gus is asleep, so a visit would wake it');
    expect(q).toContain('stays off until someone starts it');
  });

  it('keeps the old words for a running one', () => {
    expect(stopQuestion({ state: 'running', user: 'alice' })).toBe(
      'Scale alice to zero. Its data is preserved and it can be started again.',
    );
  });
});
