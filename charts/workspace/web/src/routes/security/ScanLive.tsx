import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { useState } from 'preact/hooks';
import { navigate } from '../../store/router';
import {
  SEVERITIES,
  durationLabel,
  isLiveScan,
  sortFindings,
  spendLabel,
  type ScanCounts,
  type ScanDetail,
} from '../../api/scans';
import { remove, stop } from '../../store/scans';
import { FindingDetail } from './FindingDetail';

const STATUS_TONE: Record<string, 'info' | 'success' | 'warn' | 'danger'> = {
  running: 'info',
  done: 'success',
  stopped: 'warn',
  interrupted: 'warn',
  failed: 'danger',
};

const SEVERITY_TONE: Record<string, 'danger' | 'warn' | 'info'> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warn',
  low: 'info',
};

/**
 * One scan: what it is doing, what it cost, and what it found (#726).
 *
 * The findings list is the same component whether the scan is running or
 * finished — they arrive as the scanner confirms them, so a scan in progress
 * is not a spinner with a result at the end but a list that fills in. That is
 * the difference between a multi-hour job you can watch and one you can only
 * wait for.
 */
export function ScanLive({ scan }: { scan: ScanDetail }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const live = isLiveScan(scan);
  const counts: ScanCounts =
    scan.counts ?? { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 };
  const findings = sortFindings(scan.findings ?? []);
  const dispositions = scan.dispositions ?? {};

  return (
    <div class="security-detail">
      <header class="security-detail-head">
        <div>
          <h2>
            {scan.target.name || `Port ${scan.target.port}`}{' '}
            <Pill tone={STATUS_TONE[scan.status] ?? 'neutral'}>{scan.status}</Pill>
          </h2>
          {/* Never a bare count: every failure mode here yields an empty list,
              so the sentence has to say what actually happened. */}
          <p class="security-summary">{scan.summary}</p>
        </div>
        <div class="security-detail-actions">
          {live ? (
            <MutatorOnly>
              <Button variant="danger" onClick={() => void stop(scan.id)}>
                Stop scan
              </Button>
            </MutatorOnly>
          ) : (
            <MutatorOnly>
              <Button variant="ghost" onClick={() => setConfirmDelete(true)}>
                Delete
              </Button>
            </MutatorOnly>
          )}
        </div>
      </header>

      <dl class="security-stats">
        <div>
          <dt>Address scanned</dt>
          <dd class="mono">{scan.target.url}</dd>
        </div>
        <div>
          <dt>Depth</dt>
          <dd>{scan.mode}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd class="mono">{scan.model}</dd>
        </div>
        <div>
          <dt>Spent</dt>
          <dd>{spendLabel(scan)}</dd>
        </div>
        <div>
          <dt>Running time</dt>
          <dd>{durationLabel(scan)}</dd>
        </div>
      </dl>

      {scan.error ? <pre class="security-error">{scan.error}</pre> : null}

      <section class="security-findings">
        <header class="security-findings-head">
          <h3>Findings {counts.total ? `(${counts.total})` : ''}</h3>
          <div class="security-counts">
            {SEVERITIES.filter((s) => counts[s] > 0).map((s) => (
              <Pill key={s} tone={SEVERITY_TONE[s]}>
                {counts[s]} {s}
              </Pill>
            ))}
            {counts.other > 0 ? (
              <Pill tone="neutral">{counts.other} unrated</Pill>
            ) : null}
          </div>
        </header>

        {findings.length ? (
          findings.map((f) => (
            <FindingDetail
              key={f.id}
              scanId={scan.id}
              finding={f}
              dismissed={dispositions[f.id] === 'dismissed'}
            />
          ))
        ) : (
          <p class="muted">
            {live
              ? 'Nothing found yet. Findings appear here as the scanner confirms them.'
              : 'No findings were recorded for this scan.'}
          </p>
        )}
      </section>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this scan?"
        body="Its findings will be removed from this workspace. This cannot be undone."
        confirmLabel="Delete"
        destructive
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => {
          setConfirmDelete(false);
          void remove(scan.id).then(() => navigate('/security'));
        }}
      />
    </div>
  );
}
