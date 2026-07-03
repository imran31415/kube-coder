/** In-app preview of a workspace web app, rendered in a WebView. */
import { Ionicons } from '@expo/vector-icons';
import { useNavigation, useRoute, RouteProp } from '@react-navigation/native';
import React, { useLayoutEffect, useRef } from 'react';
import { Linking, Pressable, StyleSheet, View } from 'react-native';
import { appBrowserUrl } from '../api/client';
import { AppEmbed, type AppEmbedHandle } from '../components/AppEmbed';
import type { AppsStackParams } from '../navigation';
import { colors, space } from '../theme';

export default function AppViewScreen() {
  const route = useRoute<RouteProp<AppsStackParams, 'AppView'>>();
  const nav = useNavigation();
  const { port, name } = route.params;
  const embedRef = useRef<AppEmbedHandle | null>(null);

  useLayoutEffect(() => {
    nav.setOptions({
      title: name,
      headerRight: () => (
        <View style={styles.headerBtns}>
          <Pressable
            onPress={() => embedRef.current?.reload()}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Reload app"
            style={styles.headerBtn}
          >
            <Ionicons name="refresh" size={20} color={colors.text} />
          </Pressable>
          <Pressable
            onPress={() => void Linking.openURL(appBrowserUrl(port))}
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

  return <AppEmbed port={port} name={name} embedRef={embedRef} />;
}

const styles = StyleSheet.create({
  headerBtns: { flexDirection: 'row', gap: space.md },
  headerBtn: { padding: 2 },
});
