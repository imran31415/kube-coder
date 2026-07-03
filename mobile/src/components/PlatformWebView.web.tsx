/** Web build (demo/screenshots): react-native-webview has no web support, so
 *  render a plain iframe honoring the small prop surface the app uses. Enough
 *  for the mock build to run in a browser without crashing — the live
 *  workspace flows (session cookies, websockets) are native-app territory. */
import React, { useEffect, useRef } from 'react';
import { StyleSheet, View, ViewStyle } from 'react-native';

export interface WebViewProps {
  source?: { uri: string; headers?: Record<string, string> };
  style?: ViewStyle | ViewStyle[];
  onLoadEnd?: () => void;
  onError?: (e: { nativeEvent: { description: string } }) => void;
  onHttpError?: (e: { nativeEvent: { url: string; statusCode: number } }) => void;
  // Accepted-and-ignored native-only props.
  sharedCookiesEnabled?: boolean;
  thirdPartyCookiesEnabled?: boolean;
  originWhitelist?: string[];
  allowsBackForwardNavigationGestures?: boolean;
  pullToRefreshEnabled?: boolean;
  setSupportMultipleWindows?: boolean;
  bounces?: boolean;
  overScrollMode?: string;
  startInLoadingState?: boolean;
}

export const WebView = React.forwardRef<{ reload: () => void }, WebViewProps>(
  function WebViewWeb({ source, style, onLoadEnd }, ref) {
    const frameRef = useRef<HTMLIFrameElement | null>(null);

    useEffect(() => {
      if (ref && typeof ref === 'object') {
        ref.current = {
          reload: () => {
            if (frameRef.current) frameRef.current.src = frameRef.current.src;
          },
        };
      }
    }, [ref]);

    return (
      <View style={[styles.wrap, style as ViewStyle]}>
        {source?.uri ? (
          // eslint-disable-next-line react/no-unknown-property
          <iframe
            ref={frameRef}
            src={source.uri}
            style={{ border: 0, width: '100%', height: '100%', background: '#08090b' }}
            onLoad={() => onLoadEnd?.()}
          />
        ) : null}
      </View>
    );
  },
);

const styles = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: '#08090b', overflow: 'hidden' },
});
