import { beforeEach, describe, expect, it } from 'vitest';
import {
  clearBoardFocus,
  peekBoardFocus,
  requestBoardFocus,
  _resetBoardFocusForTest,
} from './boardFocus';

beforeEach(() => {
  _resetBoardFocusForTest();
});

describe('requestBoardFocus', () => {
  it('parks the board and item for the screen to pick up', () => {
    requestBoardFocus('acme-jira', '812');
    expect(peekBoardFocus()).toMatchObject({ boardId: 'acme-jira', itemId: '812' });
  });

  it('keeps a colon-bearing item id verbatim', () => {
    /* The id is matched against item.item_id literally — encoding it, as the
       web must because its ids go into a query string, would turn
       `I_kwDOA:4102` into a string that matches no card. */
    requestBoardFocus('kube-coder-gh', 'I_kwDOA:4102');
    expect(peekBoardFocus()?.itemId).toBe('I_kwDOA:4102');
  });

  it('ignores a half-formed request instead of parking an unreachable one', () => {
    expect(requestBoardFocus('', '812')).toBeNull();
    expect(requestBoardFocus('acme-jira', '')).toBeNull();
    expect(peekBoardFocus()).toBeNull();
  });

  it('gives every request a fresh seq, so the same item twice is two requests', () => {
    const a = requestBoardFocus('acme-jira', '812');
    const b = requestBoardFocus('acme-jira', '812');
    expect(a?.seq).not.toBe(b?.seq);
    expect(peekBoardFocus()?.seq).toBe(b?.seq);
  });
});

describe('clearBoardFocus', () => {
  it('retires the request it was given', () => {
    const req = requestBoardFocus('acme-jira', '812');
    clearBoardFocus(req?.seq);
    expect(peekBoardFocus()).toBeNull();
  });

  it('lets the SECOND of two quick taps win', () => {
    /* The screen resolves request 1, and while it is scrolling the user taps a
       second notification. Clearing blind would throw away the request they are
       actually waiting on. */
    const first = requestBoardFocus('acme-jira', '812');
    const second = requestBoardFocus('kube-coder-gh', 'I_kwDOA:4102');
    clearBoardFocus(first?.seq);
    expect(peekBoardFocus()).toMatchObject({
      boardId: 'kube-coder-gh',
      itemId: 'I_kwDOA:4102',
      seq: second?.seq,
    });
  });

  it('is a no-op when nothing is pending', () => {
    expect(() => clearBoardFocus()).not.toThrow();
    expect(() => clearBoardFocus(99)).not.toThrow();
    expect(peekBoardFocus()).toBeNull();
  });

  it('clears unconditionally when no seq is given', () => {
    requestBoardFocus('acme-jira', '812');
    clearBoardFocus();
    expect(peekBoardFocus()).toBeNull();
  });
});
