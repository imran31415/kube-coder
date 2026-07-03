/** In-app preview of a workspace web app, rendered in a WebView. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import React, { useLayoutEffect, useRef, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import { WebView } from 'react-native-webview';
import { appEmbedSource, appProxyUrl } from '../api/client';
import { Button } from '../components/ui';
import { getConfig } from '../store/config';
import type { AppsStackParams } from '../navigation';
import { colors, font, radius, space } from '../theme';

export default function AppViewScreen() {
  const route = useRoute<RouteProp<AppsStackParams, 'AppView'>>();
  const nav = useNavigation();
  const { port, name } = route.params;
  const webRef = useRef<WebView>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bump to remount the WebView — a clean retry that re-runs the whole
  // session bootstrap, not just a reload of a possibly-401'd page.
  const [attempt, setAttempt] = useState(0);

  // The session bootstrap: Bearer on the first request only (all a WebView
  // can do); the server 302s into the app proxy and sets a scoped cookie the
  // embedded app's sub-resources authenticate with. In demo mode the proxy
  // is open, so point straight at it.
  const source = getConfig().mock
    ? { uri: appProxyUrl(port) }
    : appEmbedSource(port);

  useLayoutEffect(() => {
    nav.setOptions({
      title: name,
      headerRight: () => (
        <View style={styles.headerBtns}>
          <Pressable
            onPress={() => webRef.current?.reload()}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Reload app"
            style={styles.headerBtn}
          >
            <Ionicons name="refresh" size={20} color={colors.text} />
          </Pressable>
          <Pressable
            onPress={() => void Linking.openURL(appProxyUrl(port))}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Open in browser"
            style={styles.headerBtn}
          >
            <Ionicons name="open-outline" size={20} color={colors.text} />
          </Pressable>
        </View>
      ),
    });
  }, [nav, name, port]);

  if (error) {
    return (
      <View style={styles.errWrap}>
        <View style={styles.errIcon}>
          <Ionicons name="cloud-offline-outline" size={28} color={colors.danger} />
        </View>
        <Text style={styles.errTitle}>Couldn't load {name}</Text>
        <Text style={styles.errMsg}>{error}</Text>
        <Button
          title="Try again"
          icon="refresh"
          onPress={() => {
            setError(null);
            setLoading(true);
            setAttempt((a) => a + 1);
          }}
          style={{ marginTop: space.lg, alignSelf: 'stretch' }}
        />
      </View>
    );
  }

  return (
    <View style={styles.wrap}>
      <WebView
        key={attempt}
        ref={webRef}
        source={source}
        style={styles.web}
        // Share the native cookie jar so the app-session cookie set by the
        // bootstrap redirect rides on every sub-resource request.
        sharedCookiesEnabled
        thirdPartyCookiesEnabled
        // Dev servers talk websockets (HMR) and fetch from the same origin.
        originWhitelist={['*']}
        allowsBackForwardNavigationGestures
        pullToRefreshEnabled
        setSupportMultipleWindows={false}
        onLoadEnd={() => setLoading(false)}
        onError={(e) => {
          setLoading(false);
          setError(e.nativeEvent.description || 'The app did not respond.');
        }}
        onHttpError={(e) => {
          // Only the top-level document failing is fatal; a 404 favicon isn't.
          if (e.nativeEvent.url.includes(`/api/app-proxy/${port}`) && e.nativeEvent.statusCode >= 500) {
            setLoading(false);
            setError(`The app returned HTTP ${e.nativeEvent.statusCode}.`);
          }
        }}
        startInLoadingState={false}
      />
      {loading ? (
        <View style={styles.loadingBar} pointerEvents="none">
          <Text style={styles.loadingText}>Connecting to {name}…</Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: colors.bg },
  web: { flex: 1, backgroundColor: colors.bg },
  headerBtns: { flexDirection: 'row', gap: space.md },
  headerBtn: { padding: 2 },
  loadingBar: {
    position: 'absolute',
    top: space.md,
    alignSelf: 'center',
    backgroundColor: colors.bgElevated,
    borderColor: colors.border,
    borderWidth: 1,
    borderRadius: radius.pill,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  loadingText: { color: colors.textMuted, fontSize: font.size.sm, fontWeight: '600' },
  errWrap: {
    flex: 1,
    backgroundColor: colors.bg,
    alignItems: 'center',
    justifyContent: 'center',
    padding: space.xl,
  },
  errIcon: {
    width: 60,
    height: 60,
    borderRadius: radius.xl,
    backgroundColor: colors.danger + '1a',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: space.md,
  },
  errTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  errMsg: {
    color: colors.textMuted,
    fontSize: font.size.sm,
    textAlign: 'center',
    marginTop: space.sm,
    maxWidth: 300,
    lineHeight: 20,
  },
});
