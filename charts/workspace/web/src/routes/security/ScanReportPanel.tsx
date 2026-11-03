import { useMemo } from 'preact/hooks';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { pushToast } from '../../store/ui';
import type { ScanReport } from '../../api/scans';
// The one Markdown path in this SPA: it sanitises what it renders, which is
// required here — a report's text is written by a language model.
import { renderMarkdown } from '../hypervisor/transcript';

/**
 * The scanner's write-up, in a panel beside the scan: about a quarter of a
 * wide screen, stacked under the scan on a narrow one.
 *
 * It opens with where the file is saved. The report is a file on the user's
 * workspace that outlives this page, and that path is how they get back to
 * it — so it is said first, not left as a footnote under several screens of
 * text.
 */
export function ScanReportPanel({
  report,
  onClose,
}: {
  report: ScanReport;
  onClose: () => void;
}) {
  // A report runs to 256 KB, and the page re-renders on every scan update;
  // parsing and sanitising it again each time would make scrolling stutter.
  const html = useMemo(() => renderMarkdown(report.text), [report.text]);
  return (
    <aside class="scan-report" aria-label="Scanner report">
      <header class="scan-report-head">
        <h3>Scanner report</h3>
        <Button variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Hide report">
          <Icon name="close" />
        </Button>
      </header>
      {report.location ? (
        <div class="scan-report-saved">
          <Icon name="check" size={14} />
          <div class="scan-report-saved-body">
            <p>Saved in your workspace at</p>
            <code>{report.location}</code>
            <Button variant="secondary" size="sm" onClick={() => void copyPath(report.location)}>
              <Icon name="copy" size={12} />
              Copy path
            </Button>
          </div>
        </div>
      ) : null}
      <div
        class="scan-report-body"
        tabIndex={0}
        role="region"
        aria-label="Report text"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </aside>
  );
}

/** The clipboard is refused outside a secure page or without permission; the
 *  path is on screen either way, so a refusal says how to get it by hand. */
async function copyPath(path: string): Promise<void> {
  try {
    if (!navigator.clipboard) throw new Error('no clipboard');
    await navigator.clipboard.writeText(path);
    pushToast('Path copied', { kind: 'success' });
  } catch {
    pushToast('Could not copy. Select the path and copy it by hand.', { kind: 'warn' });
  }
}
