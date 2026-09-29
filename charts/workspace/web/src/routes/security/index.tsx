import { useEffect } from 'preact/hooks';
import { currentPath, navigate, pathSuffix, routeHref } from '../../store/router';
import { EmptyState } from '../../components/primitives/EmptyState';
import { Pill } from '../../components/primitives/Pill';
import { GuidePanel } from '../../components/GuidePanel';
import {
  agoLabel,
  outcomeLabel,
  spendLabel,
  type ScanSummary,
} from '../../api/scans';
import {
  connection,
  loadConnection,
  loadScan,
  loadScans,
  openScan,
  scans,
  scansDisabled,
  scansLoaded,
  watchScans,
} from '../../store/scans';
import { ConnectStrix } from './ConnectStrix';
import { ScanForm } from './ScanForm';
import { ScanLive } from './ScanLive';
import './security.css';

const STATUS_TONE: Record<string, 'info' | 'success' | 'warn' | 'danger'> = {
  running: 'info',
  done: 'success',
  stopped: 'warn',
  interrupted: 'warn',
  failed: 'danger',
};

/**
 * Security scanning (#726).
 *
 * `/security` lists scans and starts one; `/security/<id>` is one scan. The
 * whole surface hides when the deployment has scanning switched off, because
 * it needs a container runtime the workspace may not have — see the
 * `strix.enabled` notes in values.yaml.
 */
export function SecurityRoute() {
  const suffix = pathSuffix(currentPath.value).split('/')[0];
  const scanId = suffix.startsWith('scn_') ? suffix : null;

  useEffect(() => {
    void loadConnection();
    void loadScans();
    return watchScans();
  }, []);

  useEffect(() => {
    if (scanId) void loadScan(scanId);
  }, [scanId]);

  if (scansDisabled.value) {
    return (
      <div class="route route-security">
        <header class="route-header">
          <h1 class="route-title">Security</h1>
        </header>
        <EmptyState
          title="Scanning is switched off in this workspace"
          description={
            <>
              Scanning runs its tools inside a container, so it has to be turned
              on when the workspace is deployed. Ask whoever runs this cluster
              to enable it.
            </>
          }
        />
      </div>
    );
  }

  if (scanId) {
    const scan = openScan.value;
    return (
      <div class="route route-security">
        <header class="route-header">
          <h1 class="route-title">
            <a
              class="security-back"
              href="/security"
              onClick={(e) => {
                e.preventDefault();
                navigate('/security');
              }}
            >
              Security
            </a>{' '}
            <span class="muted">/ scan</span>
          </h1>
        </header>
        {scan && scan.id === scanId ? (
          <ScanLive scan={scan} />
        ) : (
          <p class="muted">Loading…</p>
        )}
      </div>
    );
  }

  const conn = connection.value;
  const rows = scans.value;

  return (
    <div class="route route-security">
      <header class="route-header">
        <h1 class="route-title">Security</h1>
        <p class="route-subtitle muted">
          Check an app you are running here for security holes, before anyone
          else does.
        </p>
      </header>

      <GuidePanel
        title="How scanning works"
        storageKey="kc.security.guide"
        intro={
          <>
            An AI security tester attacks your running app the way an intruder
            would, and only reports problems it managed to prove.
          </>
        }
        steps={[
          {
            title: 'You pick an app',
            body: 'Anything listening in this workspace. Nothing outside it can be scanned.',
          },
          {
            title: 'It tries to break in',
            body: 'It sends real attack traffic — bad logins, injected input, unexpected requests — and watches how the app responds.',
          },
          {
            title: 'It proves what it finds',
            body: 'A finding arrives with the steps to reproduce it, so you can see the problem yourself rather than take its word.',
          },
          {
            title: 'You decide what to do',
            body: 'Findings are sorted worst-first. Dismiss the ones that do not apply; the rest stay until you deal with them.',
          },
        ]}
        scenarios={[
          {
            prompt: 'Quick scan, about 5 minutes',
            outcome: 'A fast pass for the obvious problems — good before a demo.',
          },
          {
            prompt: 'Deep scan, 1 to 4 hours',
            outcome: 'A thorough review that costs correspondingly more.',
          },
        ]}
      />

      <p class="muted">
        <a
          href={routeHref('/docs/security-scanning')}
          onClick={(e) => {
            e.preventDefault();
            navigate('/docs/security-scanning');
          }}
        >
          More about scanning — what it costs, what it sees, and what it cannot
          do yet
        </a>
      </p>

      {conn && !conn.configured ? <ConnectStrix /> : <ScanForm />}

      <section class="security-list">
        <h2>Past scans</h2>
        {!scansLoaded.value ? (
          <p class="muted">Loading…</p>
        ) : rows.length ? (
          <ul class="security-rows">
            {rows.map((scan) => (
              <ScanRow key={scan.id} scan={scan} />
            ))}
          </ul>
        ) : (
          <p class="muted">No scans yet.</p>
        )}
      </section>

      {conn?.configured ? (
        <details class="security-settings">
          <summary>Model settings</summary>
          <ConnectStrix />
        </details>
      ) : null}
    </div>
  );
}

function ScanRow({ scan }: { scan: ScanSummary }) {
  return (
    <li class="security-row">
      <a
        href={`/security/${scan.id}`}
        onClick={(e) => {
          e.preventDefault();
          navigate(`/security/${scan.id}`);
        }}
      >
        <span class="security-row-main">
          <Pill tone={STATUS_TONE[scan.status] ?? 'neutral'}>{scan.status}</Pill>
          <strong>{scan.target.name || `Port ${scan.target.port}`}</strong>
          {/* Outcome, not a bare number — an empty findings list means very
              different things depending on how the scan ended. */}
          <span class="security-row-outcome">{outcomeLabel(scan)}</span>
        </span>
        <span class="security-row-meta muted">
          {scan.mode} · {spendLabel(scan)} · {agoLabel(scan.started_at)}
        </span>
      </a>
    </li>
  );
}
