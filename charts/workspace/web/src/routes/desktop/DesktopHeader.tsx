import { useEffect, useState } from 'preact/hooks';
import { getGithubFullStatus, githubDisplayName } from '../../api/github';
import { Icon } from '../../components/Icon';

/**
 * Desktop identity header — the clean masthead at the very top of the page.
 * Shows the workspace operator's GitHub handle under an "AI Workspace"
 * eyebrow, with a gradient monogram. The name resolves from the gh CLI
 * login (falling back to the git user name); if the status call fails —
 * e.g. an unauthenticated read-only visitor gets a 401 — it degrades to a
 * neutral "Workspace" label rather than showing anything empty.
 */
export function DesktopHeader() {
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGithubFullStatus()
      .then((s) => { if (!cancelled) setName(githubDisplayName(s)); })
      .catch(() => { /* leave null → neutral label */ });
    return () => { cancelled = true; };
  }, []);

  const display = name ?? 'Workspace';
  const initial = display.charAt(0).toUpperCase();

  return (
    <header class="dt-hero" data-dt-stop="true">
      <div class="dt-hero-avatar" aria-hidden="true">
        <span>{initial}</span>
      </div>
      <div class="dt-hero-text">
        <span class="dt-hero-eyebrow">AI Workspace</span>
        <span class="dt-hero-name">
          {display}
          {name && (
            <span class="dt-hero-badge" title="Signed-in identity">
              <Icon name="github" size={13} />
            </span>
          )}
        </span>
      </div>
    </header>
  );
}
