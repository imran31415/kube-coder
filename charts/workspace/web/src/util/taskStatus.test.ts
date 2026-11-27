import { describe, expect, it } from 'vitest';
import { isAliveStatus, isFailedStatus, isParkedStatus } from './taskStatus';

describe('isAliveStatus', () => {
  it.each(['running', 'waiting-for-input'])('%s is alive', (s) => {
    expect(isAliveStatus(s)).toBe(true);
  });

  it.each(['completed', 'error', 'killed', 'terminated', 'parked', 'unknown', '', undefined])(
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

  it.each(['running', 'waiting-for-input', 'completed', 'parked', 'unknown', undefined])(
    '%s did not fail',
    (s) => {
      expect(isFailedStatus(s)).toBe(false);
    },
  );
});

describe('isParkedStatus', () => {
  it('is true only for parked (#928)', () => {
    expect(isParkedStatus('parked')).toBe(true);
    for (const s of ['running', 'waiting-for-input', 'completed', 'killed', 'terminated', undefined]) {
      expect(isParkedStatus(s)).toBe(false);
    }
  });
});
