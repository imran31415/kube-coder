import { useState } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Pill } from '../../components/primitives/Pill';
import { MutatorOnly } from '../../components/MutatorOnly';
import {
  FINDING_FACTS,
  FINDING_SECTIONS,
  extraFields,
  type Finding,
} from '../../api/scans';
import { dismissFinding } from '../../store/scans';

const TONES: Record<string, 'danger' | 'warn' | 'info' | 'neutral'> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warn',
  low: 'info',
};

function text(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

/**
 * One finding, in the scanner's own words (#726).
 *
 * Nothing here rewrites, summarises or re-scores what the scanner reported —
 * this is a security claim about the user's app, and paraphrasing it would put
 * words the tool never said next to its name. Known fields get a heading in a
 * sensible reading order; anything else the scanner reported is still shown
 * underneath, so a newer scanner version never has evidence silently dropped.
 *
 * The long parts start collapsed: a reproduction script can be pages, and a
 * list of findings where every one is fully expanded cannot be scanned by eye.
 */
export function FindingDetail({
  scanId,
  finding,
  dismissed,
}: {
  scanId: string;
  finding: Finding;
  dismissed: boolean;
}) {
  const [open, setOpen] = useState(false);
  const severity = (finding.severity || '').toLowerCase();
  const facts = FINDING_FACTS.map((f) => ({ ...f, value: text(finding[f.key]) }))
    .filter((f) => f.value);
  const sections = FINDING_SECTIONS.map((s) => ({ ...s, value: text(finding[s.key]) }))
    .filter((s) => s.value);
  const extras = extraFields(finding);

  return (
    <article class={`finding ${dismissed ? 'is-dismissed' : ''}`}>
      <header class="finding-head">
        <button
          type="button"
          class="finding-toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <Pill tone={TONES[severity] ?? 'neutral'}>
            {severity || 'unrated'}
          </Pill>
          <span class="finding-title">{text(finding.title) || finding.id}</span>
          <span class="finding-chevron" aria-hidden>{open ? '▾' : '▸'}</span>
        </button>
        <MutatorOnly>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              void dismissFinding(scanId, finding.id, dismissed ? 'open' : 'dismissed')
            }
          >
            {dismissed ? 'Restore' : 'Dismiss'}
          </Button>
        </MutatorOnly>
      </header>

      {facts.length ? (
        <div class="finding-facts">
          {facts.map((f) => (
            <span key={f.key} class="finding-fact">
              <span class="muted">{f.label}:</span> {f.value}
            </span>
          ))}
        </div>
      ) : null}

      {open ? (
        <div class="finding-body">
          {sections.map((s) => (
            <section key={s.key}>
              <h4>{s.label}</h4>
              {s.code ? <pre><code>{s.value}</code></pre> : <p>{s.value}</p>}
            </section>
          ))}
          {extras.length ? (
            <section>
              <h4>Also reported</h4>
              <dl class="finding-extras">
                {extras.map((e) => (
                  <div key={e.key}>
                    <dt>{e.key}</dt>
                    <dd><pre><code>{e.value}</code></pre></dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}
          {!sections.length && !extras.length ? (
            <p class="muted">The scanner recorded no further detail.</p>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
