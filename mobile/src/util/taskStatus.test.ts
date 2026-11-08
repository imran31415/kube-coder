import { describe, expect, it } from 'vitest';
import { isActiveStatus, normStatus } from './taskStatus';

describe('normStatus', () => {
  it.each([
    ['waiting-for-input', 'waiting'],
    ['waiting_for_input', 'waiting'],
    ['waiting_input', 'waiting'],
    ['completed', 'done'],
    ['running', 'running'],
    ['error', 'error'],
    ['killed', 'killed'],
    ['terminated', 'terminated'],
  ])('%s -> %s', (wire, app) => {
    expect(normStatus(wire)).toBe(app);
  });

  it('turns a missing status into an empty string', () => {
    expect(normStatus(undefined)).toBe('');
  });
});

describe('isActiveStatus', () => {
  it.each(['running', 'waiting'])('%s is active', (s) => {
    expect(isActiveStatus(s)).toBe(true);
  });

  it.each(['done', 'error', 'killed', 'terminated', '', undefined])('%s is not active', (s) => {
    expect(isActiveStatus(s)).toBe(false);
  });
});
