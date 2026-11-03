import { describe, expect, it, afterEach } from 'vitest';
import { vncUrl, vncWsPath } from './tasks';

type WinPrefix = { __KC_AUTH_PREFIX__?: string };
const setPrefix = (v: string) => {
  (window as WinPrefix).__KC_AUTH_PREFIX__ = v;
};

afterEach(() => {
  delete (window as WinPrefix).__KC_AUTH_PREFIX__;
});

describe('vncWsPath', () => {
  // noVNC builds `wss://<host>:<port>/<path>` from its `path` setting and
  // ignores the directory the viewer was served from, so this value is the
  // whole reason the WebSocket reaches websockify at all.
  it('carries the oauth prefix so the socket reaches the /oauth/websockify ingress', () => {
    expect(vncWsPath('/oauth')).toBe('oauth/websockify');
  });

  it('is the plain noVNC default under basic auth, leaving that mode unchanged', () => {
    // Basic-auth deployments route a literal /websockify straight to :6081
    // with no rewrite, which is already correct — this must not change it.
    expect(vncWsPath('')).toBe('websockify');
  });

  it('never emits a leading slash, which noVNC would double', () => {
    // ui.js does `url += '/' + path`; a leading slash yields '//websockify'.
    for (const prefix of ['/oauth', '', '//oauth']) {
      expect(vncWsPath(prefix).startsWith('/')).toBe(false);
    }
  });
});

describe('vncUrl', () => {
  it('tells noVNC the prefixed socket path in oauth2 mode', () => {
    setPrefix('/oauth');
    const url = vncUrl();
    expect(url.startsWith('/oauth/vnc-direct/vnc.html?')).toBe(true);
    // Encoded, because it is a query-parameter value containing a slash.
    expect(url).toContain(`path=${encodeURIComponent('oauth/websockify')}`);
    expect(new URLSearchParams(url.split('?')[1]).get('path')).toBe('oauth/websockify');
  });

  it('asks for the default path under basic auth', () => {
    setPrefix('');
    const url = vncUrl();
    expect(url.startsWith('/vnc-direct/vnc.html?')).toBe(true);
    expect(new URLSearchParams(url.split('?')[1]).get('path')).toBe('websockify');
  });

  it('keeps the viewer options it already passed', () => {
    setPrefix('/oauth');
    const q = new URLSearchParams(vncUrl().split('?')[1]);
    expect(q.get('autoconnect')).toBe('true');
    expect(q.get('resize')).toBe('scale');
    expect(q.get('view_clip')).toBe('true');
    expect(q.get('t')).toBeTruthy();
  });
});
