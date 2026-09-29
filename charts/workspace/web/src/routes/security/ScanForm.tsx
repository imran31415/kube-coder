import { useEffect, useState } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Input } from '../../components/primitives/Input';
import { MutatorOnly } from '../../components/MutatorOnly';
import { navigate } from '../../store/router';
import { MODE_LABELS, type ScanMode } from '../../api/scans';
import { connection, loadTargets, startScan, targets } from '../../store/scans';

/**
 * Start a scan (#726).
 *
 * Three deliberate choices, each preventing a specific way of wasting money:
 *
 * * **The target is picked from a list**, never typed. The list comes from the
 *   apps actually listening in this workspace, and an app that the scanner
 *   cannot reach is shown with the reason rather than silently offered.
 * * **Depth carries its time cost** in the label. The scanner's own default is
 *   its deepest mode — hours of work — so the shallow one leads here.
 * * **The budget field is always visible**, and says plainly what an empty one
 *   means. It is optional, as it is in the scanner, but never invisible.
 */
export function ScanForm() {
  const [port, setPort] = useState<number | null>(null);
  const [mode, setMode] = useState<ScanMode>('quick');
  const [budget, setBudget] = useState('5');
  const [instruction, setInstruction] = useState('');
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    void loadTargets();
  }, []);

  const rows = targets.value;
  const chosen = rows.find((t) => t.port === port) ?? null;
  const conn = connection.value;

  // Pre-select the first app the scanner can actually reach.
  useEffect(() => {
    if (port === null && rows.length) {
      setPort((rows.find((t) => t.reachable) ?? rows[0]).port);
    }
  }, [rows.length]);

  if (!rows.length) {
    return (
      <section class="security-start">
        <h2>Scan an app</h2>
        <p class="muted">
          Nothing is running in this workspace yet. Start your app, then come
          back — or open{' '}
          <a
            href="/apps"
            onClick={(e) => {
              e.preventDefault();
              navigate('/apps');
            }}
          >
            Apps
          </a>{' '}
          to see what is listening.
        </p>
      </section>
    );
  }

  async function submit(e: Event) {
    e.preventDefault();
    if (port === null) return;
    setStarting(true);
    const id = await startScan({
      port,
      mode,
      budget_usd: budget.trim() === '' ? null : Number(budget),
      instruction: instruction.trim(),
    });
    setStarting(false);
    if (id) navigate(`/security/${id}`);
  }

  return (
    <section class="security-start">
      <h2>Scan an app</h2>
      <form class="security-form" onSubmit={submit}>
        <label class="security-field">
          <span>App</span>
          <select
            class="input"
            value={port === null ? '' : String(port)}
            onChange={(e) => setPort(Number((e.target as HTMLSelectElement).value))}
          >
            {rows.map((t) => (
              <option key={t.port} value={String(t.port)}>
                {t.name ? `${t.name} — port ${t.port}` : `Port ${t.port}`}
                {t.reachable ? '' : ' (not reachable)'}
              </option>
            ))}
          </select>
          {chosen && !chosen.reachable ? (
            <small class="security-warn">{chosen.reason}</small>
          ) : null}
        </label>

        <label class="security-field">
          <span>How deep</span>
          <select
            class="input"
            value={mode}
            onChange={(e) => setMode((e.target as HTMLSelectElement).value as ScanMode)}
          >
            {(Object.keys(MODE_LABELS) as ScanMode[]).map((m) => (
              <option key={m} value={m}>
                {MODE_LABELS[m]}
              </option>
            ))}
          </select>
        </label>

        <label class="security-field">
          <span>Spending limit (US$)</span>
          <Input
            value={budget}
            inputMode="decimal"
            placeholder="No limit"
            onInput={(e) => setBudget((e.target as HTMLInputElement).value)}
          />
          <small class={budget.trim() === '' ? 'security-warn' : 'muted'}>
            {budget.trim() === ''
              ? 'With no limit the scan runs until it finishes. A deep scan can take hours and spend accordingly.'
              : 'The scan stops cleanly when it reaches this much.'}
          </small>
        </label>

        <label class="security-field">
          <span>Anything specific to look at <span class="muted">(optional)</span></span>
          <Input
            value={instruction}
            placeholder="e.g. focus on the login and checkout pages"
            onInput={(e) => setInstruction((e.target as HTMLInputElement).value)}
          />
        </label>

        <div class="security-actions">
          <MutatorOnly>
            <Button
              type="submit"
              variant="primary"
              disabled={starting || port === null || !conn?.configured}
            >
              {starting ? 'Starting…' : 'Start scan'}
            </Button>
          </MutatorOnly>
          {conn && !conn.configured ? (
            <span class="muted">Connect a model first.</span>
          ) : null}
        </div>
        <p class="muted security-fineprint">
          The scan sends real attack traffic to the app you pick. Only scan
          something you own. Its model provider sees what the scanner sends and
          receives while it works.
        </p>
      </form>
    </section>
  );
}
