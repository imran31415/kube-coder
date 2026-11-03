import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { useState } from 'preact/hooks';
import { navigate } from '../../store/router';
import {
  SEVERITIES,
  STATUS_LABELS,
  budgetFraction,
  durationLabel,
  emptyFindingsText,
  isLiveScan,
  modePhrase,
  moneyLabel,
  scanTitle,
  sortFindings,
  startedLabel,
  type ScanCounts,
  type ScanDetail,
} from '../../api/scans';
import { remove, stop } from '../../store/scans';
import { Icon } from '../../components/Icon';
import { FindingDetail } from './FindingDetail';
import { ScanOutcome } from './ScanOutcome';
import { ScanReportPanel } from './ScanReportPanel';

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
 * One scan: which app, how far along, what it cost, and what it found (#726).
 *
 * The page leads with the app's name and keeps the address the scanner used
 * out of sight: it is an internal container address the user never chose and
 * cannot open, so it lives under "Scan details" for whoever is debugging.
 *
 * The findings list is the same component whether the scan is running or
 * finished — they arrive as the scanner confirms them, so a scan in progress
 * is not a spinner with a result at the end but a list that fills in. That is
 * the difference between a multi-hour job you can watch and one you can only
 * wait for.
 */
export function ScanLive({ scan }: { scan: ScanDetail }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [reportOpen, setReportOpen] = useState(true);
  const report = scan.report;
  const live = isLiveScan(scan);
  const counts: ScanCounts =
    scan.counts ?? { critical: 0, high: 0, medium: 0, low: 0, other: 0, total: 0 };
  const findings = sortFindings(scan.findings ?? []);
  const dispositions = scan.dispositions ?? {};
  const title = scanTitle(scan);
  const spent = scan.usage?.cost_usd ?? 0;
  const fraction = budgetFraction(scan);

  async function confirmAndStop() {
    setConfirmStop(false);
    setStopping(true);
    try {
      await stop(scan.id);
    } finally {
      setStopping(false);
    }
  }

  return (
    <div class={report && reportOpen ? 'scan-layout has-report' : 'scan-layout'}>
      <div class="security-detail">
        <header class="security-detail-head">
          <div>
            <h2 class="scan-title">
              <span>{title}</span>
              {live ? (
                <span class="scan-live">
                  <span class="scan-pulse" aria-hidden="true" />
                  Scanning
                </span>
              ) : (
                <Pill tone={STATUS_TONE[scan.status] ?? 'neutral'}>
                  {STATUS_LABELS[scan.status] ?? scan.status}
                </Pill>
              )}
            </h2>
            <p class="scan-sub">
              {/* The port is already the title when the app has no name. */}
              {scan.target.name ? <span>Port {scan.target.port}</span> : null}
              <span>{modePhrase(scan.mode)}</span>
              <span>{startedLabel(scan.started_at)}</span>
            </p>
          </div>
          <div class="security-detail-actions">
            {report && !reportOpen ? (
              <Button onClick={() => setReportOpen(true)}>
                <Icon name="files" size={14} />
                Show report
              </Button>
            ) : null}
            {live ? (
              <MutatorOnly>
                {/* Disabled while the request is out: the scanner gets several
                    seconds to exit cleanly, and a button that still looks
                    pressable then invites a second press. */}
                <Button
                  variant="danger"
                  disabled={stopping}
                  onClick={() => setConfirmStop(true)}
                >
                  {stopping ? 'Stopping…' : 'Stop scan'}
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

        {/* Never a bare count: every failure mode here yields an empty list,
            so a finished scan says how it ended and why, high on the page.
            While it runs, the header already says so. */}
        {live ? null : <ScanOutcome scan={scan} />}

        <dl class="scan-stats" aria-label="Progress">
          <div>
            <dt>Problems found</dt>
            <dd class={counts.total ? 'is-problem' : undefined}>{counts.total}</dd>
          </div>
          <div>
            <dt>{live ? 'Running for' : 'Took'}</dt>
            <dd>{durationLabel(scan)}</dd>
          </div>
          <div>
            <dt>Spent</dt>
            <dd>
              {moneyLabel(spent)}
              {scan.budget_usd ? (
                <small> of {moneyLabel(scan.budget_usd)}</small>
              ) : null}
            </dd>
            {fraction !== null ? (
              <div
                class="scan-meter"
                role="meter"
                aria-label="Budget used"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(fraction * 100)}
              >
                <span style={{ width: `${fraction * 100}%` }} />
              </div>
            ) : (
              <small class="muted">No budget cap</small>
            )}
          </div>
        </dl>

        {/* What the meter's number MEANT. The figure alone does not say that
            the scanner stops short of the cap, so a scan that ended on budget
            reads as one that simply finished. */}
        {scan.budget_note ? (
          <p class="scan-budget-note">{scan.budget_note}</p>
        ) : null}

        <section class="security-findings">
          <header class="security-findings-head">
            <h3>Problems found {counts.total ? `(${counts.total})` : ''}</h3>
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
            <p class="scan-empty">{emptyFindingsText(scan)}</p>
          )}
        </section>

        <details class="scan-details">
          <summary>Scan details</summary>
          <dl>
            <div>
              <dt>Depth</dt>
              <dd>{modePhrase(scan.mode)}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd class="mono">{scan.model}</dd>
            </div>
            <div>
              <dt>Budget</dt>
              <dd>
                {scan.budget_usd
                  ? `${moneyLabel(scan.budget_usd)} (stops automatically)`
                  : 'No cap'}
              </dd>
            </div>
            <div>
              <dt>Address scanned</dt>
              <dd class="mono">{scan.target.url}</dd>
            </div>
          </dl>
        </details>
      </div>

      {/* The scanner's write-up: the result the scan was actually run for.
          It used to be dropped, so a finished scan with nothing proven
          showed a blank page. Beside the scan rather than under it, so it
          can be read alongside the findings it explains. */}
      {report && reportOpen ? (
        <ScanReportPanel report={report} onClose={() => setReportOpen(false)} />
      ) : null}

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this scan?"
        body={
          report
            ? 'Its findings and its saved report will be removed from this workspace. This cannot be undone.'
            : 'Its findings will be removed from this workspace. This cannot be undone.'
        }
        confirmLabel="Delete"
        destructive
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => {
          setConfirmDelete(false);
          void remove(scan.id).then(() => navigate('/security'));
        }}
      />

      <ConfirmDialog
        open={confirmStop}
        title="Stop this scan?"
        body="Anything found so far is kept. The rest of the app will not be checked, and a stopped scan cannot be resumed."
        confirmLabel="Stop scan"
        destructive
        onCancel={() => setConfirmStop(false)}
        onConfirm={() => void confirmAndStop()}
      />
    </div>
  );
}
