import { describe, expect, it } from 'vitest';
import { isAliveStatus, isFailedStatus } from './taskStatus';

describe('isAliveStatus', () => {
  it.each(['running', 'waiting-for-input'])('%s is alive', (s) => {
    expect(isAliveStatus(s)).toBe(true);
  });

  it.each(['completed', 'error', 'killed', 'terminated', 'unknown', '', undefined])(
    '%s is not alive',
    (s) => {
      expect(isAliveStatus(s)).toBe(false);
    },
  );
});

describe('isFailedStatus', () => {
  it.each(['error', 'killed', 'terminated'])('%s failed', (s) => {
    expect(isFailedStatus(s)).toBe(true);
  });

  it.each(['running', 'waiting-for-input', 'completed', 'unknown', undefined])(
    '%s did not fail',
    (s) => {
      expect(isFailedStatus(s)).toBe(false);
    },
  );
});
