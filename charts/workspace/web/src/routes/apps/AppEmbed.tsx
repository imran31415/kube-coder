import { useEffect, useMemo, useState } from 'preact/hooks';
import { navigate } from '../../store/router';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { type AppEntry, proxyUrl } from '../../api/apps';
import './apps.css';

/** Renders a single embedded application in an iframe.
 *
 *  The server-side proxy strips X-Frame-Options + CSP frame-ancestors
 *  from the upstream response so the iframe can mount even when the
 *  upstream app would normally refuse framing. Auth flows through the
 *  /oauth ingress prefix — `proxyUrl` already includes that prefix. */
export function AppEmbed({ port, appsList }: { port: number; appsList: AppEntry[] }) {
  const entry = useMemo(() => appsList.find((a) => a.port === port), [appsList, port]);
  // Use a key on the iframe so "Reload" can trigger a fresh mount
  // (browsers don't always re-fetch on src reassignment).
  const [reloadKey, setReloadKey] = useState(0);

  // When the route enters /apps/<port>, the dashboard's main content
  // wrapper still pads + scrolls. We want the iframe to fill, so add a
  // class to body while mounted that removes those margins on mobile.
  useEffect(() => {
    document.body.classList.add('apps-embedding');
    return () => document.body.classList.remove('apps-embedding');
  }, []);

  const url = proxyUrl(port);

  return (
    <div class="apps-embed">
      <header class="apps-embed-header">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apps')} aria-label="Back to list">
          <Icon name="chevron-left" size={14} /> Back
        </Button>
        <div class="apps-embed-title">
          {entry?.name || `Port ${port}`}
          <span class="muted apps-embed-port"> :{port}</span>
        </div>
        <div class="apps-embed-actions">
          <Button variant="ghost" size="sm" onClick={() => setReloadKey((k) => k + 1)} title="Reload">
            Reload
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => window.open(url, '_blank', 'noopener')}
            title="Open in new tab"
          >
            <Icon name="link" size={14} /> New tab
          </Button>
        </div>
      </header>
      <iframe
        key={reloadKey}
        class="apps-embed-frame"
        src={url}
        title={entry?.name || `Application on port ${port}`}
        // The proxy already enforces same-origin auth + loopback-only
        // upstreams. sandbox=allow-same-origin so cookies/localStorage
        // work for typical dev servers; allow-scripts/forms for normal
        // app behaviour. No allow-top-navigation: a misbehaving app
        // can't jump the user out of the dashboard.
        sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-downloads"
      />
    </div>
  );
}
