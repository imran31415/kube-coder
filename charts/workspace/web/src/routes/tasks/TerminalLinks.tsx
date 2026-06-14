import { useState } from 'preact/hooks';
import { Icon } from '../../components/Icon';
import { shortenUrl } from './terminalUrls';

/**
 * Auto-detected links from a session's output, rendered as a compact badge
 * that expands on tap. Collapsed by default so the link list never eats the
 * terminal/chat viewport; tapping reveals each URL with open + copy. Shared by
 * the Session tab (TerminalPane) and the Send-message tab (MessageChat) so the
 * link affordance is identical in both places.
 */
export function TerminalLinks({
  urls,
  label = 'Links from session',
}: {
  urls: string[];
  label?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);

  if (urls.length === 0) return null;

  async function copyUrl(u: string) {
    try {
      await navigator.clipboard.writeText(u);
      setCopiedUrl(u);
      setTimeout(() => setCopiedUrl((cur) => (cur === u ? null : cur)), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  const count = urls.length;
  const badgeText = count === 1 ? '1 link detected' : `${count} links detected`;

  if (!expanded) {
    return (
      <div class="tlinks">
        <button
          type="button"
          class="tlinks-badge"
          onClick={() => setExpanded(true)}
          title="Show detected links"
          aria-expanded={false}
        >
          <Icon name="link" size={11} />
          <span>{badgeText}</span>
          <Icon name="chevron-down" size={11} />
        </button>
      </div>
    );
  }

  return (
    <div class="tlinks tlinks-open" aria-label={label}>
      <div class="tlinks-head">
        <span class="tlinks-label muted">{label}</span>
        <button
          type="button"
          class="tlinks-collapse"
          onClick={() => setExpanded(false)}
          title="Hide links"
          aria-expanded
        >
          Hide
        </button>
      </div>
      <ul class="tlinks-list">
        {urls.map((u) => (
          <li key={u} class="tlinks-item">
            <a
              class="tlinks-link"
              href={u}
              target="_blank"
              rel="noopener noreferrer"
              title={u}
            >
              <Icon name="chevron-right" size={11} />
              <span class="mono">{shortenUrl(u)}</span>
            </a>
            <button
              type="button"
              class="tlinks-copy"
              onClick={() => void copyUrl(u)}
              title="Copy URL to clipboard"
              aria-label={`Copy ${u}`}
            >
              {copiedUrl === u ? 'copied' : 'copy'}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
