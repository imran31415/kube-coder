import { usePollingResource } from '../hooks/usePollingResource';
import { ResourceStatus } from './ResourceStatus';
import { getInsights } from '../api/workspaces';
import { navigate } from '../router';

const ICON: Record<string, string> = { critical: '⛔', warn: '⚠', info: 'ℹ' };

/** Polls every minute; failed refreshes retain explicitly marked previous data. */
export function InsightsBar() {
  const resource = usePollingResource('insights', signal => getInsights(signal), 60000);
  const adv = resource.data?.advisories ?? [];
  if (!resource.data || adv.length === 0) return <div class="insights">
    <ResourceStatus {...resource} />
    {resource.data && !resource.problem && <div class="insight calm">All workspaces look healthy.</div>}
  </div>;
  return (
    <div class="insights" aria-label="Insights">
      <ResourceStatus {...resource} />
      {adv.map((a, i) => (
        <button
          key={`${a.user}-${a.kind}-${i}`}
          class={`insight sev-${a.severity}`}
          onClick={() => navigate(`/w/${a.user}`)}
          title={`Open ${a.user}`}
        >
          <span class="insight-icon" aria-hidden="true">{ICON[a.severity] ?? 'ℹ'}</span>
          <span class="insight-msg">{a.message}</span>
        </button>
      ))}
    </div>
  );
}
