import React, { useEffect, useRef, useState } from 'react';
import { Linking, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import { ApiError, buildPublishAction, getBuildPublish, getBuildPublishDiff } from '../api/client';
import type { PublishStatus } from '../api/publishTypes';
import { colors } from '../theme';
import { Button } from './ui';

export type PublishAction = { title: string; disabled: boolean; onPress: () => void };
type Props = { taskId: string; onAction?: (action: PublishAction | null) => void };
export function BuildPublishPanel({ taskId, onAction }: Props) {
  return <Review key={taskId} taskId={taskId} onAction={onAction} />;
}
function Review({ taskId, onAction }: Props) {
  const [status, setStatus] = useState<PublishStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [draft, setDraft] = useState(false);
  const [remote, setRemote] = useState('');
  const [base, setBase] = useState('');
  const [options, setOptions] = useState(false);
  const [files, setFiles] = useState(false);
  const [diff, setDiff] = useState('');
  const alive = useRef(true);
  const working = useRef(false);
  const loaded = useRef('');
  const editRevision = useRef(0);
  const request = useRef(0);
  function accept(s: PublishStatus, ownSave = false) {
    if (!alive.current) return;
    setStatus(s);
    const p = s.preparation;
    if (p && (loaded.current !== p.id || ownSave)) {
      loaded.current = p.id; editRevision.current = p.draft_revision; setTitle(p.title); setBody(p.body); setDraft(p.draft); setDiff('');
    }
  }
  useEffect(() => {
    alive.current = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { if (!working.current) accept(await getBuildPublish(taskId)); }
      catch (e) { if (alive.current && !(e instanceof ApiError && e.status === 404)) setError(e instanceof Error ? e.message : String(e)); }
      finally { if (alive.current) timer = setTimeout(poll, 3000); }
    }
    void poll();
    return () => { alive.current = false; clearTimeout(timer); };
  }, [taskId]);
  async function act(fn: () => Promise<PublishStatus>) {
    if (working.current) return;
    working.current = true; setBusy(true); setError('');
    try { accept(await fn(), true); }
    catch (e) { if (alive.current) setError(e instanceof Error ? e.message : 'Connection interrupted. Reopen the Build to check saved progress.'); }
    finally { working.current = false; if (alive.current) setBusy(false); }
  }
  const p = status?.preparation;
  const active = busy || !!status?.preparing || !!status?.operation && !['failed', 'published'].includes(status.operation.stage);
  const completed = !!p && !!status?.pr && status.operation?.stage === 'published' && status.operation.preparation_id === p.id;
  const action: PublishAction | null = completed ? {
    title: 'View PR', disabled: false,
    onPress: () => { void Linking.openURL(status!.pr!.url).catch(() => setError('Could not open GitHub')); },
  } : p && status ? {
    title: active ? 'Publishing…' : status.pr ? 'Push updates' : 'Push & Open PR',
    disabled: active || !title.trim() || !status.eligibility.can_publish || status.operation?.stage === 'published' && status.operation.preparation_id === p.id,
    onPress: () => void act(publish),
  } : null;
  useEffect(() => { onAction?.(action); }, [status, busy, title, body, draft, onAction]);
  useEffect(() => () => onAction?.(null), [onAction]);
  if (!status?.supported) return error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null;
  const changed = p && (title !== p.title || body !== p.body || draft !== p.draft);
  const save = () => buildPublishAction(taskId, 'draft', { preparation_id: p!.id, draft_revision: editRevision.current, title, body, draft });
  async function publish() {
    const s = changed ? await save() : status!;
    accept(s, true);
    const prepared = s.preparation!;
    return buildPublishAction(taskId, '', { preparation_id: prepared.id, fingerprint: prepared.fingerprint,
      draft_revision: prepared.draft_revision, idempotency_key: `phone-${Date.now()}-${Math.random().toString(36).slice(2)}` });
  }
  return <View style={styles.panel}>
    <Text style={styles.heading}>Publish this Build</Text>
    <Text style={styles.text}>Review changes, then commit this snapshot and open a GitHub PR.</Text>
    {!!status.eligibility.reason && <Text style={styles.text}>{status.eligibility.reason.message}</Text>}
    {!!(error || status.preparation_error) && <Text accessibilityRole="alert" style={styles.error}>{error || status.preparation_error?.message}</Text>}
    {!!status.pr && <Button title={`Open PR #${status.pr.number} · ${status.pr.state}`} onPress={() => { void Linking.openURL(status.pr!.url).catch(() => setError('Could not open GitHub')); }} />}
    {!!status.operation && <Text accessibilityLiveRegion="polite" style={styles.text}>Publishing: {status.operation.stage.replaceAll('_', ' ')}</Text>}
    {!!status.operation?.error && <Text style={styles.error}>{status.operation.error.message}</Text>}
    {status.operation?.stage === 'failed' && <Button title="Retry saved publish" disabled={active} onPress={() => void act(() => buildPublishAction(taskId, `${status.operation!.id}/retry`))} />}
    <Button title="Destination options" variant="secondary" onPress={() => setOptions(!options)} />
    {options && <>
      <Text style={styles.text}>Push remote</Text><TextInput style={styles.input} accessibilityLabel="Push remote" value={remote} onChangeText={setRemote} autoCapitalize="none" editable={!active} />
      <Text style={styles.text}>Base branch</Text><TextInput style={styles.input} accessibilityLabel="Base branch" value={base} onChangeText={setBase} autoCapitalize="none" editable={!active} />
    </>}
    <Button title={status.preparing ? 'Preparing review…' : p ? 'Refresh review' : 'Prepare review'} variant="secondary" disabled={active || !status.eligibility.can_prepare} onPress={() => void act(() => buildPublishAction(taskId, 'prepare', { destination: { remote, base_branch: base } }))} />
    {p && <>
      <Text selectable style={styles.text}>{p.destination.head_repo}:{p.destination.head_branch} → {p.destination.base_repo}:{p.destination.base_branch}</Text>
      <Text style={styles.text}>Author: {p.author.name} &lt;{p.author.email}&gt; · GitHub: {p.destination.identity}</Text>
      <Text style={styles.text}>Tests: {p.checks.state}. Review the Build log.</Text>
      {!!p.summary_error && <Text style={styles.text}>{p.summary_error}</Text>}
      <Button title={`Reviewed snapshot · ${p.files.length} files`} variant="secondary" onPress={() => setFiles(!files)} />
      {files && <>
        {p.files.map(f => <Pressable key={f.path} accessibilityRole="button" accessibilityLabel={`Review ${f.path}`} style={styles.file} onPress={async () => {
          const n = ++request.current; setDiff('Loading…');
          try { const r = await getBuildPublishDiff(taskId, p.id, f.path); if (alive.current && n === request.current) setDiff(r.diff + (r.truncated ? '\n[Diff truncated]' : '')); }
          catch (e) { if (alive.current && n === request.current) setDiff(String(e)); }
        }}><Text style={styles.text}>{f.path} {f.binary ? '(binary)' : `+${f.added} −${f.deleted}`}</Text></Pressable>)}
        {!!diff && <ScrollView horizontal style={{ maxHeight: 300 }}><Text selectable style={styles.text}>{diff}</Text></ScrollView>}
      </>}
      <Text style={styles.text}>PR title</Text><TextInput accessibilityLabel="PR title" style={styles.input} value={title} onChangeText={setTitle} maxLength={200} editable={!active} />
      <Text style={styles.text}>PR summary</Text><TextInput accessibilityLabel="PR summary" style={[styles.input, { minHeight: 160, textAlignVertical: 'top' }]} value={body} onChangeText={setBody} maxLength={20000} multiline editable={!active} />
      <View style={styles.row}><Text style={styles.text}>Create as draft PR</Text><Switch accessibilityLabel="Create as draft PR" value={draft} onValueChange={setDraft} disabled={active} /></View>
      <Button title="Save description" variant="secondary" disabled={active || !changed || !title.trim()} onPress={() => void act(save)} />
      {!onAction && action && <Button {...action} />}
    </>}
  </View>;
}
const styles = StyleSheet.create({
  panel: { padding: 12, gap: 12, borderColor: colors.border, borderWidth: 1, marginVertical: 12 },
  heading: { color: colors.text, fontSize: 18, fontWeight: '700' },
  text: { color: colors.text, fontSize: 15 }, error: { color: colors.danger, fontSize: 15 },
  input: { color: colors.text, backgroundColor: colors.surface2, borderColor: colors.border, borderWidth: 1, minHeight: 44, padding: 10, fontSize: 16 },
  file: { minHeight: 44, justifyContent: 'center', borderBottomColor: colors.border, borderBottomWidth: 1 },
  row: { minHeight: 44, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
});
