import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/preact';
import { MutatorOnly, ReadOnlyOnly, ReadOnlyPill } from './MutatorOnly';
import { serverMode } from '../store/server-mode';

describe('MutatorOnly / ReadOnlyOnly / ReadOnlyPill', () => {
  beforeEach(() => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic' };
  });

  it('MutatorOnly renders children when not read-only', () => {
    const r = render(<MutatorOnly><button>Delete</button></MutatorOnly>);
    expect(r.getByText('Delete')).toBeTruthy();
  });

  it('MutatorOnly hides children when read-only', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'none' };
    const r = render(<MutatorOnly><button>Delete</button></MutatorOnly>);
    expect(r.queryByText('Delete')).toBeNull();
  });

  it('ReadOnlyOnly is the inverse — hides children outside read-only mode', () => {
    const r = render(<ReadOnlyOnly><div>demo banner</div></ReadOnlyOnly>);
    expect(r.queryByText('demo banner')).toBeNull();
  });

  it('ReadOnlyOnly renders children inside read-only mode', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'none' };
    const r = render(<ReadOnlyOnly><div>demo banner</div></ReadOnlyOnly>);
    expect(r.getByText('demo banner')).toBeTruthy();
  });

  it('ReadOnlyPill renders only in read-only mode', () => {
    let r = render(<ReadOnlyPill />);
    expect(r.queryByText(/Demo/)).toBeNull();
    r.unmount();

    serverMode.value = { readOnly: true, authed: true, authMode: 'none' };
    r = render(<ReadOnlyPill />);
    expect(r.getByText(/Demo · Deploy your own/)).toBeTruthy();
  });

  it('ReadOnlyPill in demo mode links to the kube-coder repo (deploy-your-own CTA)', () => {
    serverMode.value = { readOnly: true, authed: true, authMode: 'none' };
    const r = render(<ReadOnlyPill />);
    const link = r.container.querySelector('a.readonly-pill') as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.href).toMatch(/github\.com\/imran31415\/kube-coder/);
    expect(link.target).toBe('_blank');
    // noopener guards against tabnabbing on the demo deploy.
    expect(link.rel).toMatch(/noopener/);
  });
});
