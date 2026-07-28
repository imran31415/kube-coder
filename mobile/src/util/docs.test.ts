import { describe, expect, it } from 'vitest';
import type { DocsManifest } from '../api/types';
import { filterDocs, flattenDocs, groupDocs, stripLeadingTitle } from './docs';

const manifest: DocsManifest = {
  version: 1,
  sections: [
    {
      id: 'overview',
      title: 'Overview',
      pages: [
        {
          id: 'getting-started',
          title: 'Getting started',
          file: 'in-app/getting-started.md',
          summary: 'Connect the app and ship your first build.',
        },
      ],
    },
    {
      id: 'triggers',
      title: 'Triggers',
      pages: [
        { id: 'triggers-webhooks', title: 'Webhooks', file: 'in-app/triggers-webhooks.md' },
        { id: 'triggers-crons', title: 'Crons', file: 'in-app/triggers-crons.md', summary: 'Scheduled builds.' },
      ],
    },
  ],
};

describe('flattenDocs', () => {
  it('flattens pages and carries the section down each row', () => {
    const rows = flattenDocs(manifest);
    expect(rows.map((r) => r.id)).toEqual(['getting-started', 'triggers-webhooks', 'triggers-crons']);
    expect(rows[1]).toMatchObject({ section_id: 'triggers', section_title: 'Triggers' });
  });

  it('survives a null manifest and empty/missing sections', () => {
    expect(flattenDocs(null)).toEqual([]);
    expect(flattenDocs({ version: 1, sections: [] })).toEqual([]);
    expect(
      flattenDocs({ version: 1, sections: [{ id: 'x', title: 'X', pages: [] }] }),
    ).toEqual([]);
  });
});

describe('filterDocs', () => {
  const rows = flattenDocs(manifest);

  it('matches title, summary, section and id', () => {
    expect(filterDocs(rows, 'webhook').map((r) => r.id)).toEqual(['triggers-webhooks']);
    expect(filterDocs(rows, 'first build').map((r) => r.id)).toEqual(['getting-started']);
    expect(filterDocs(rows, 'Triggers').map((r) => r.id)).toEqual([
      'triggers-webhooks',
      'triggers-crons',
    ]);
  });

  it('returns everything for a blank query and nothing for a miss', () => {
    expect(filterDocs(rows, '  ')).toHaveLength(3);
    expect(filterDocs(rows, 'kubernetes operator')).toEqual([]);
  });
});

describe('groupDocs', () => {
  it('regroups rows into sections in manifest order', () => {
    const grouped = groupDocs(flattenDocs(manifest));
    expect(grouped.map((s) => s.id)).toEqual(['overview', 'triggers']);
    expect(grouped[1].pages.map((p) => p.id)).toEqual(['triggers-webhooks', 'triggers-crons']);
  });

  it('drops sections the filter emptied', () => {
    const grouped = groupDocs(filterDocs(flattenDocs(manifest), 'webhook'));
    expect(grouped.map((s) => s.id)).toEqual(['triggers']);
    expect(grouped[0].pages).toHaveLength(1);
  });
});

describe('stripLeadingTitle', () => {
  it('drops a leading H1 that repeats the page title', () => {
    expect(stripLeadingTitle('# Webhooks\n\nA webhook turns…', 'Webhooks')).toBe(
      'A webhook turns…',
    );
    // Case and inner whitespace shouldn't decide it.
    expect(stripLeadingTitle('#   getting   started\nBody', 'Getting started')).toBe('Body');
  });

  it('keeps an H1 that says something the title does not', () => {
    const md = '# Signing\n\nEvery webhook…';
    expect(stripLeadingTitle(md, 'Webhooks')).toBe(md);
  });

  it('leaves pages that open with prose or a lower heading alone', () => {
    expect(stripLeadingTitle('Intro paragraph\n\n# Webhooks', 'Webhooks')).toBe(
      'Intro paragraph\n\n# Webhooks',
    );
    expect(stripLeadingTitle('## Webhooks\n\nBody', 'Webhooks')).toBe('## Webhooks\n\nBody');
  });
});
