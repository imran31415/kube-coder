import type { ComponentChildren } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import {
  type SpendResponse,
  type TokenBlock,
  type WorkspaceSpend,
  type ModelSpend,
  getSpend,
} from '../api/spend';
import { fmtTokens } from '../format';

const RANGES: [string, number][] = [
  ['6h', 21600],
  ['24h', 86400],
  ['7d', 604800],
];

const CLASSES: [keyof TokenBlock, string][] = [
  ['input', 'Input'],
  ['cache_read', 'Cache read'],
  ['cache_write', 'Cache write'],
  ['output', 'Output'],
];

/** Fleet-wide agent token spend (#581) — the operator-plane half of the
 *  per-workspace measurement in #573/#575.
 *
 *  Three states look identical if you only render numbers, so each is called
 *  out explicitly: no Prometheus at all, a Prometheus that nothing is exporting
 *  these series to, and a genuinely quiet fleet. A zero an operator trusts is
 *  worse than no number. */
export function SpendPanel() {
  const [spend, setSpend] = useState<SpendResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [range, setRange] = useState(21600);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const r = await getSpend(range);
        if (!alive) return;
        setSpend(r);
        setErr(null);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      }
    }
    void load();
    const id = window.setInterval(() => void load(), 30000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  if (err && !spend) {
    return (
      <Frame range={range} setRange={setRange} open={open} setOpen={setOpen}>
        <div class="panel-msg err">Couldn't load agent spend: {err}</div>
      </Frame>
    );
  }
  if (!spend) {
    return (
      <Frame range={range} setRange={setRange} open={open} setOpen={setOpen}>
        <div class="panel-msg">Loading agent spend…</div>
      </Frame>
    );
  }

  const cov = spend.coverage.window;
  const unmeasured = cov.not_instrumented + cov.no_session_id;

  return (
    <Frame
      range={range}
      setRange={setRange}
      open={open}
      setOpen={setOpen}
      sub={`${spend.workspacesReporting} workspace${spend.workspacesReporting === 1 ? '' : 's'} reporting`}
    >
      {spend.metricsError && (
        <div class="panel-msg err">
          Spend metrics require Prometheus: {spend.metricsError}
        </div>
      )}
      {!spend.metricsError && spend.scrapeHint && (
        <div class="panel-msg err">No workspace is exporting spend metrics. {spend.scrapeHint}</div>
      )}

      {open && !spend.metricsError && (
        <>
          <div class="spend-total">
            <span class="spend-total-val">{fmtTokens(spend.fleet.window.total)}</span>
            <span class="spend-total-unit">tokens</span>
            {spend.fleet.window.unclassified > 0 && (
              <span class="spend-note">
                + {fmtTokens(spend.fleet.window.unclassified)} unclassified (not priceable)
              </span>
            )}
          </div>
          <div class="spend-classes">
            {CLASSES.map(([key, label]) => (
              <div class="spend-class" key={key}>
                <span class="spend-class-name">{label}</span>
                <span class="spend-class-val">{fmtTokens(spend.fleet.window[key])}</span>
              </div>
            ))}
          </div>

          {/* The zero-disambiguator: only Claude Code reports token usage, so a
              run on any other assistant contributes a 0 that means "unknown".
              Stating it beside the total is what keeps the total honest. */}
          <div class="spend-coverage">
            {cov.measured + unmeasured === 0
              ? 'No agent runs in this window.'
              : `${cov.measured} of ${cov.measured + unmeasured} runs measurable` +
                (unmeasured > 0
                  ? ` — ${unmeasured} ran on an assistant that reports no usage, so their spend is unknown, not zero.`
                  : '.')}
          </div>

          <Table
            title="By workspace"
            empty="No workspace recorded spend in this window."
            rows={spend.byWorkspace.map((w: WorkspaceSpend) => [w.user, w.window])}
          />
          <Table
            title="By model"
            empty="No model breakdown in this window."
            rows={spend.byModel.map((m: ModelSpend) => [m.model, m.window])}
          />
        </>
      )}
    </Frame>
  );
}

function Frame({
  children,
  range,
  setRange,
  open,
  setOpen,
  sub,
}: {
  children: ComponentChildren;
  range: number;
  setRange: (n: number) => void;
  open: boolean;
  setOpen: (fn: (o: boolean) => boolean) => void;
  sub?: string;
}) {
  return (
    <section class="cap spend">
      <div class="cap-hd">
        <button class="cap-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span class={`chev ${open ? 'open' : ''}`} aria-hidden="true">▸</span>
          <h2>Agent spend</h2>
        </button>
        {sub && <span class="cap-sub">{sub}</span>}
        <div class="cap-ranges">
          {RANGES.map(([label, secs]) => (
            <button
              key={secs}
              class={`chip ${range === secs ? 'on' : ''}`}
              onClick={() => setRange(secs)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {children}
    </section>
  );
}

function Table({
  title,
  empty,
  rows,
}: {
  title: string;
  empty: string;
  rows: [string, TokenBlock][];
}) {
  return (
    <div class="spend-table">
      <div class="cap-nodes-hd">{title}</div>
      {rows.length === 0 ? (
        <div class="panel-msg">{empty}</div>
      ) : (
        rows.map(([name, b]) => (
          <div class="spend-row" key={name}>
            <span class="spend-row-name">{name}</span>
            <span class="spend-row-classes">
              {CLASSES.map(([key, label]) => (
                <span class="spend-row-class" key={key} title={label}>
                  {label.toLowerCase()} {fmtTokens(b[key])}
                </span>
              ))}
            </span>
            <span class="spend-row-total">{fmtTokens(b.total)}</span>
          </div>
        ))
      )}
    </div>
  );
}
