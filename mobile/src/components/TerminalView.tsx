/** The task-detail output surface.
 *
 *  Live mode renders the workspace's real ttyd terminal (xterm.js) in a
 *  WebView — the exact same session the web dashboard shows, so Claude's TUI
 *  renders pixel-faithfully instead of through a lossy ANSI re-implementation.
 *  Flow: POST prepare-terminal (marks this task's tmux session as pending) →
 *  session-cookie bootstrap → /api/terminal-proxy (server-side proxy to ttyd)
 *  → terminal-entry.sh attaches the pending session.
 *
 *  When the tmux session no longer exists (finished/old tasks) there is
 *  nothing live to attach, so it falls back to the archived output record,
 *  rendered with the lightweight ANSI parser under an "archived" notice.
 */
import { Ionicons } from '@expo/vector-icons';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { WebView } from 'react-native-webview';
import { prepareTerminal, terminalEmbedSource } from '../api/client';
import { getConfig } from '../store/config';
import { parseAnsiLines } from '../util/ansi';
import { colors, font, radius, space } from '../theme';
import { Button } from './ui';

type Mode = 'connecting' | 'live' | 'archived' | 'error';

export function TerminalView({ taskId, output }: { taskId: string; output: string }) {
  const [mode, setMode] = useState<Mode>('connecting');
  const [errMsg, setErrMsg] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [webviewLoading, setWebviewLoading] = useState(true);
  // Computed once per attempt: the bootstrap URL is cache-busted (ttyd re-runs
  // its entrypoint per connection), so it must not change on re-renders or the
  // WebView reconnects in a loop.
  const source = useMemo(
    () => terminalEmbedSource(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [attempt],
  );

  const connect = useCallback(async () => {
    setMode('connecting');
    if (getConfig().mock) {
      setMode('archived');
      return;
    }
    try {
      const r = await prepareTerminal(taskId);
      if (r.session_ready) {
        setWebviewLoading(true);
        setAttempt((a) => a + 1);
        setMode('live');
      } else {
        // Session gone — finished task; the record is all there is.
        setMode('archived');
      }
    } catch (e) {
      setErrMsg((e as Error).message);
      setMode('error');
    }
  }, [taskId]);

  useEffect(() => {
    void connect();
  }, [connect]);

  if (mode === 'connecting') {
    return (
      <View style={styles.center}>
        <View style={styles.pill}>
          <Text style={styles.pillText}>Attaching to session…</Text>
        </View>
      </View>
    );
  }

  if (mode === 'error') {
    return (
      <View style={styles.center}>
        <View style={styles.errIcon}>
          <Ionicons name="cloud-offline-outline" size={26} color={colors.danger} />
        </View>
        <Text style={styles.errTitle}>Couldn't open the session</Text>
        <Text style={styles.errMsg}>{errMsg}</Text>
        <View style={styles.errBtns}>
          <Button title="Try again" icon="refresh" onPress={() => void connect()} style={{ flex: 1 }} />
          <Button
            title="Archived output"
            variant="secondary"
            onPress={() => setMode('archived')}
            style={{ flex: 1 }}
          />
        </View>
      </View>
    );
  }

  if (mode === 'archived') {
    return <ArchivedOutput output={output} />;
  }

  return (
    <View style={styles.wrap}>
      <WebView
        key={attempt}
        source={source}
        style={styles.web}
        sharedCookiesEnabled
        thirdPartyCookiesEnabled
        originWhitelist={['*']}
        // The terminal owns every gesture: no rubber-banding or pull-to-refresh
        // fighting xterm.js scrollback.
        bounces={false}
        overScrollMode="never"
        pullToRefreshEnabled={false}
        setSupportMultipleWindows={false}
        allowsBackForwardNavigationGestures={false}
        onLoadEnd={() => setWebviewLoading(false)}
        onError={(e) => {
          setErrMsg(e.nativeEvent.description || 'The terminal did not respond.');
          setMode('error');
        }}
        onHttpError={(e) => {
          if (e.nativeEvent.statusCode >= 500) {
            setErrMsg(`Terminal returned HTTP ${e.nativeEvent.statusCode}.`);
            setMode('error');
          }
        }}
      />
      {webviewLoading ? (
        <View style={styles.centerOverlay} pointerEvents="none">
          <View style={styles.pill}>
            <Text style={styles.pillText}>Attaching to session…</Text>
          </View>
        </View>
      ) : null}
    </View>
  );
}

/** Finished/old tasks: the recorded output, ANSI-colored, pinned to bottom. */
function ArchivedOutput({ output }: { output: string }) {
  const scrollRef = useRef<ScrollView>(null);
  const pinnedToBottom = useRef(true);
  const lines = useMemo(() => parseAnsiLines(output), [output]);

  return (
    <View style={styles.wrap}>
      <View style={styles.archivedBar}>
        <Ionicons name="time-outline" size={13} color={colors.textFaint} />
        <Text style={styles.archivedText}>Archived output — the live session has ended</Text>
      </View>
      <ScrollView
        ref={scrollRef}
        style={styles.term}
        contentContainerStyle={styles.termContent}
        scrollEventThrottle={64}
        onScroll={(e) => {
          const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
          pinnedToBottom.current =
            contentOffset.y + layoutMeasurement.height >= contentSize.height - 40;
        }}
        onContentSizeChange={() => {
          if (pinnedToBottom.current) scrollRef.current?.scrollToEnd({ animated: false });
        }}
      >
        {lines.length === 0 ? (
          <Text style={styles.termText}>(no output recorded)</Text>
        ) : (
          lines.map((line, i) => (
            <Text key={i} style={styles.termText}>
              {line.length === 0
                ? ' '
                : line.map((seg, j) => (
                    <Text
                      key={j}
                      style={{
                        color: seg.color ?? '#c8d3df',
                        fontWeight: seg.bold ? '700' : '400',
                        opacity: seg.dim ? 0.6 : 1,
                      }}
                    >
                      {seg.text}
                    </Text>
                  ))}
            </Text>
          ))
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: '#08090b' },
  web: { flex: 1, backgroundColor: '#08090b' },
  center: {
    flex: 1,
    backgroundColor: '#08090b',
    alignItems: 'center',
    justifyContent: 'center',
    padding: space.xl,
    gap: space.sm,
  },
  centerOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: 'center',
    justifyContent: 'center',
  },
  pill: {
    backgroundColor: colors.bgElevated,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.pill,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  pillText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  errIcon: {
    width: 56,
    height: 56,
    borderRadius: radius.xl,
    backgroundColor: colors.danger + '1a',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: space.xs,
  },
  errTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '700' },
  errMsg: {
    color: colors.textMuted,
    fontSize: font.size.sm,
    textAlign: 'center',
    maxWidth: 300,
    lineHeight: 19,
  },
  errBtns: { flexDirection: 'row', gap: space.sm, marginTop: space.md, alignSelf: 'stretch' },
  archivedBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    paddingVertical: 6,
    backgroundColor: colors.bgElevated,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  archivedText: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.4,
  },
  term: { flex: 1 },
  termContent: { padding: space.lg },
  termText: { color: '#c8d3df', fontFamily: font.mono, fontSize: font.size.sm, lineHeight: 19 },
});
