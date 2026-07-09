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
import { useIsFocused } from '@react-navigation/native';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState, ScrollView, StyleSheet, Text, View } from 'react-native';
import { WebView } from './PlatformWebView';
import { prepareTerminal, terminalEmbedSource } from '../api/client';
import { getConfig } from '../store/config';
import { parseAnsiLines } from '../util/ansi';
import { colors, font, radius, space } from '../theme';
import { Button } from './ui';

type Mode = 'connecting' | 'live' | 'archived' | 'error';

/** Injected into the terminal WebView before ttyd's client runs. ttyd's own
 *  auto-reconnect gives up when the socket dies with an `error` event (it sets
 *  doReconnect=false) or a clean close, then waits for an Enter keypress that
 *  never comes on touch. Wrapping WebSocket lets the native side observe every
 *  close/error and remount the WebView instead — the mobile equivalent of the
 *  web dashboard's reload. Idempotent: both injection points run it. */
const WS_WATCH_JS = `
(function () {
  if (window.__kcWsWatch) return;
  window.__kcWsWatch = true;
  var notify = function () {
    try { window.ReactNativeWebView.postMessage('kc-ws-dead'); } catch (e) {}
  };
  var NativeWS = window.WebSocket;
  if (!NativeWS) return;
  var Wrapped = function (url, protocols) {
    var ws = protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
    ws.addEventListener('close', notify);
    ws.addEventListener('error', notify);
    return ws;
  };
  Wrapped.prototype = NativeWS.prototype;
  Wrapped.CONNECTING = NativeWS.CONNECTING;
  Wrapped.OPEN = NativeWS.OPEN;
  Wrapped.CLOSING = NativeWS.CLOSING;
  Wrapped.CLOSED = NativeWS.CLOSED;
  window.WebSocket = Wrapped;
})();
true;`;

// Auto-reconnect pacing: ignore ws-dead signals within DEBOUNCE of the last
// remount (ttyd churns the socket during a normal mount), and give up into
// the error state after MAX_BURST remounts inside BURST_WINDOW so a broken
// upstream doesn't loop the WebView forever.
const RECONNECT_DEBOUNCE_MS = 3000;
const RECONNECT_BURST_WINDOW_MS = 30_000;
const RECONNECT_MAX_BURST = 4;

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

  // Guard: auto-reconnects only fire while this task's screen is focused and
  // showing the live terminal. Reconnecting re-writes the one-shot pending
  // file (prepare-terminal), which would steal the next ttyd attach from
  // whichever task the user is actually looking at.
  const isFocused = useIsFocused();
  const guardRef = useRef({ focused: isFocused, live: false });
  guardRef.current = { focused: isFocused, live: mode === 'live' };

  // Timestamps of recent auto-reconnects, for debounce + runaway-loop cutoff.
  const reconnectsRef = useRef<number[]>([]);
  const autoReconnect = useCallback(() => {
    const g = guardRef.current;
    if (!g.focused || !g.live) return;
    if (AppState.currentState !== 'active') return; // resume handler covers this
    const now = Date.now();
    const recent = reconnectsRef.current.filter((t) => now - t < RECONNECT_BURST_WINDOW_MS);
    if (recent.length > 0 && now - recent[recent.length - 1] < RECONNECT_DEBOUNCE_MS) return;
    if (recent.length >= RECONNECT_MAX_BURST) {
      reconnectsRef.current = recent;
      setErrMsg('The terminal keeps disconnecting.');
      setMode('error');
      return;
    }
    recent.push(now);
    reconnectsRef.current = recent;
    void connect();
  }, [connect]);

  // The OS suspends the WebView while the app is backgrounded, killing ttyd's
  // WebSocket. On return to the foreground, remount — same fix as reloading
  // the page on the web dashboard. Also fires when the user comes back to a
  // still-mounted screen after a background trip elsewhere in the app: the
  // wentBackground flag survives until the next focused resume or reconnect.
  const wentBackgroundRef = useRef(false);
  useEffect(() => {
    const sub = AppState.addEventListener('change', (state) => {
      if (state !== 'active') {
        wentBackgroundRef.current = true;
        return;
      }
      if (!wentBackgroundRef.current) return;
      const g = guardRef.current;
      if (!g.focused || !g.live) return;
      wentBackgroundRef.current = false;
      reconnectsRef.current = [];
      void connect();
    });
    return () => sub.remove();
  }, [connect]);

  // Navigating back to this screen after the app was backgrounded while it
  // wasn't focused: the AppState handler skipped the reconnect, so do it now.
  useEffect(() => {
    if (!isFocused || !wentBackgroundRef.current) return;
    if (guardRef.current.live) {
      wentBackgroundRef.current = false;
      reconnectsRef.current = [];
      void connect();
    }
  }, [isFocused, connect]);

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
    // The demo build renders the recorded transcript as-is — labelling it
    // "archived" there would be confusing when the task reads as running.
    return <ArchivedOutput output={output} showNotice={!getConfig().mock} />;
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
        // Both injection points run the (idempotent) WebSocket watch:
        // beforeContentLoaded hooks the constructor before ttyd's client
        // connects; the post-load variant is the fallback on platforms where
        // the early injection is flaky (older Android WebView).
        injectedJavaScriptBeforeContentLoaded={WS_WATCH_JS}
        injectedJavaScript={WS_WATCH_JS}
        onMessage={(e) => {
          if (e.nativeEvent.data === 'kc-ws-dead') autoReconnect();
        }}
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
function ArchivedOutput({ output, showNotice = true }: { output: string; showNotice?: boolean }) {
  const scrollRef = useRef<ScrollView>(null);
  const pinnedToBottom = useRef(true);
  const lines = useMemo(() => parseAnsiLines(output), [output]);

  return (
    <View style={styles.wrap}>
      {showNotice ? (
        <View style={styles.archivedBar}>
          <Ionicons name="time-outline" size={13} color={colors.textFaint} />
          <Text style={styles.archivedText}>Archived output — the live session has ended</Text>
        </View>
      ) : null}
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
                        color: seg.color ?? colors.text,
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
  wrap: { flex: 1, backgroundColor: colors.bg },
  web: { flex: 1, backgroundColor: colors.bg },
  center: {
    flex: 1,
    backgroundColor: colors.bg,
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
  termText: { color: colors.text, fontFamily: font.mono, fontSize: font.size.sm, lineHeight: 19 },
});
