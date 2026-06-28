import type { ExpoConfig } from 'expo/config';

/**
 * Expo app config. Drives `eas build --profile production` and store submission.
 *
 * Before the first cloud build, run `eas init` (or `eas build`) once while
 * logged in — it creates the EAS project and writes `extra.eas.projectId`
 * here. Set EAS_PROJECT_ID in the environment to pin it in CI.
 */
const config: ExpoConfig = {
  name: 'kube-coder',
  slug: 'kube-coder-mobile',
  version: '1.0.0',
  orientation: 'portrait',
  icon: './assets/icon.png',
  scheme: 'kubecoder',
  userInterfaceStyle: 'dark',
  backgroundColor: '#0b0d10',
  assetBundlePatterns: ['**/*'],
  ios: {
    supportsTablet: true,
    bundleIdentifier: 'app.kubecoder.mobile',
    buildNumber: '1',
    infoPlist: {
      // The app talks to a user-supplied https workspace host; no cleartext.
      ITSAppUsesNonExemptEncryption: false,
    },
  },
  android: {
    package: 'app.kubecoder.mobile',
    versionCode: 1,
    adaptiveIcon: {
      foregroundImage: './assets/android-icon-foreground.png',
      backgroundColor: '#0b0d10',
    },
  },
  web: {
    favicon: './assets/favicon.png',
    bundler: 'metro',
  },
  plugins: [
    'expo-secure-store',
    [
      'expo-splash-screen',
      {
        image: './assets/splash-icon.png',
        resizeMode: 'contain',
        backgroundColor: '#0b0d10',
      },
    ],
  ],
  extra: {
    eas: {
      projectId: process.env.EAS_PROJECT_ID ?? undefined,
    },
  },
  owner: process.env.EAS_OWNER ?? undefined,
};

export default config;
