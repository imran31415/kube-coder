/**
 * A navigator screen whose code loads the first time it is shown (#833).
 * See src/screenRegistry.ts for why and which screens.
 *
 * The navigator gets an ordinary component, so it passes `navigation` and
 * `route` through as usual and a deep link to a screen that has never been
 * opened just mounts it: the chunk loads, then the screen renders with its
 * params. Until it lands, the pane shows the app background (a local chunk
 * takes a frame or two, so a spinner would only flash).
 */
import React, { Suspense, type ComponentType } from 'react';
import { StyleSheet, View } from 'react-native';

import { screenLoaders, type LazyScreenName } from '../screenRegistry';
import { colors } from '../theme';

function Loading() {
  return <View style={styles.loading} />;
}

// One React.lazy per screen for the life of the app: a second instance
// would load (and render) the module again.
const cache = new Map<LazyScreenName, ComponentType<object>>();

export function lazyScreen(name: LazyScreenName): ComponentType<object> {
  const hit = cache.get(name);
  if (hit) return hit;
  const Lazy = React.lazy(screenLoaders[name]);
  function Screen(props: object) {
    return (
      <Suspense fallback={<Loading />}>
        <Lazy {...props} />
      </Suspense>
    );
  }
  Screen.displayName = `Lazy(${name})`;
  cache.set(name, Screen);
  return Screen;
}

const styles = StyleSheet.create({
  loading: { flex: 1, backgroundColor: colors.bg },
});
