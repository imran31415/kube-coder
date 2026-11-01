import { useState } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';
import { outcomeView, type ScanDetail } from '../../api/scans';
import { openModelSettings, rerun } from '../../store/scans';

/**
 * How a finished scan ended, and what to do next — one card (#726).
 *
 * Always shown once a scan stops, because every way it can end leaves the
 * problems list looking the same: a headline in the user's terms, the
 * server's one honest sentence, the reason when something went wrong, and
 * at most two next steps. The raw scanner output is never here; it goes to
 * the workspace server log, where whoever debugs it will look.
 */
export function ScanOutcome({ scan }: { scan: ScanDetail }) {
  const [starting, setStarting] = useState(false);
  const view = outcomeView(scan);
  const caveats = scan.caveats ?? [];

  async function again() {
    setStarting(true);
    try {
      await rerun(scan);
    } finally {
      setStarting(false);
    }
  }

  return (
    <section class={`scan-outcome is-${view.tone}`} aria-label="How this scan ended">
      <span class="scan-outcome-icon" aria-hidden="true">
        <Icon name={view.tone === 'success' ? 'check' : 'info'} size={18} />
      </span>
      <div class="scan-outcome-body">
        <h3>{view.headline}</h3>
        <p>{scan.summary}</p>
        {scan.error ? <p class="scan-outcome-reason">{scan.error}</p> : null}
        {/* The scanner's own account of what it never got to: an empty
            problems list under this means "not checked", not "clean". */}
        {caveats.length ? (
          <ul class="scan-caveats">
            {caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : null}
        {view.action || view.settings ? (
          <MutatorOnly>
            <div class="scan-outcome-actions">
              {view.action ? (
                <Button
                  size="sm"
                  variant={view.action === 'retry' ? 'primary' : 'secondary'}
                  disabled={starting}
                  onClick={() => void again()}
                >
                  {starting ? 'Starting…' : view.action === 'retry' ? 'Try again' : 'Scan again'}
                </Button>
              ) : null}
              {view.settings ? (
                <Button size="sm" variant="ghost" onClick={openModelSettings}>
                  Model settings
                </Button>
              ) : null}
            </div>
          </MutatorOnly>
        ) : null}
      </div>
    </section>
  );
}
