import type { PublishStatus } from '../api/publishTypes';

const states = new Map<string, PublishStatus>();
export function mockPublish(id: string): PublishStatus {
  if (!states.has(id)) states.set(id, {
    supported: true, eligibility: { can_prepare: id === 'publish-demo', can_publish: false,
      reason: id === 'publish-demo' ? null : { code: 'writer_active', message: 'End the Build session before reviewing changes for publication.' } },
    preparing: null, preparation_error: null, preparation: null, operation: null, pr: null,
  });
  return JSON.parse(JSON.stringify(states.get(id)));
}
export function mockPublishAction(id: string, action: string, body: unknown): PublishStatus {
  const s = mockPublish(id);
  const b = body as Record<string, unknown>;
  if (action === 'prepare') {
    s.eligibility.can_publish = true;
    s.preparation = { id: 'prep-demo', fingerprint: 'demo-snapshot', draft_revision: 1,
      title: 'Add a health check endpoint', body: '## Changes\n\nAdd /healthz and a regression test.\n\n## Testing\n\nResults for this exact revision are unknown.',
      draft: false, summary_status: 'ready', author: { name: 'Demo developer', email: 'demo@example.test' }, checks: { state: 'unknown' },
      destination: { head_repo: 'demo/example', head_branch: 'kc/health-check', base_repo: 'demo/example', base_branch: 'develop', identity: 'personal:demo' },
      files: [{ path: 'server.py', added: 8, deleted: 0, binary: false }, { path: 'tests/health_test.py', added: 12, deleted: 0, binary: false }],
    };
  } else if (action === 'draft' && s.preparation) {
    if (b.draft_revision !== s.preparation.draft_revision) throw new Error('Description changed on another device.');
    s.preparation = { ...s.preparation, title: String(b.title), body: String(b.body), draft: b.draft === true, draft_revision: s.preparation.draft_revision + 1 };
  } else if (!action && s.preparation) {
    s.operation = { id: 'pub-demo', preparation_id: s.preparation.id, stage: 'published', commit_sha: 'a1b2c3d' };
    s.pr = { number: 42, url: 'https://github.com/demo/example/pull/42', state: 'open', draft: s.preparation.draft, head_sha: 'a1b2c3d' };
  }
  states.set(id, s);
  return mockPublish(id);
}
