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
      ITSAppUsesNonExemptEncryption: false,
      // The user points the app at THEIR OWN kube-coder host. Production hosts
      // are HTTPS (cert-manager), but a self-hosted or local (minikube,
      // port-forwarded) workspace is plain HTTP on a LAN/localhost address — so
      // we allow arbitrary loads. Hosts are operator-supplied, never hardcoded.
      NSAppTransportSecurity: {
        NSAllowsArbitraryLoads: true,
        NSAllowsLocalNetworking: true,
      },
    },
  },
  android: {
    package: 'app.kubecoder.mobile',
    versionCode: 1,
    // Cleartext HTTP is enabled via the expo-build-properties plugin below
    // (same rationale as the iOS ATS exception above: self-hosted / localhost).
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
      // Allow plain-HTTP requests on Android (self-hosted / localhost minikube
      // workspaces). Production cloud hosts still use HTTPS.
      'expo-build-properties',
      { android: { usesCleartextTraffic: true } },
    ],
    [
      'expo-splash-screen',
      {
        image: './assets/splash-icon.png',
        resizeMode: 'contain',
        backgroundColor: '#0b0d10',
      },
    ],
    [
      // Photo-library + camera access for attaching images to a task's
      // follow-up (issue #179): pick from the library or take a photo.
      'expo-image-picker',
      {
        photosPermission:
          'Allow kube-coder to attach photos from your library to a task.',
        cameraPermission:
          'Allow kube-coder to take a photo to attach to a task.',
      },
    ],
  ],
  extra: {
    eas: {
      // EAS project for @develop.imran/kube-coder-mobile. Override with
      // EAS_PROJECT_ID in CI to target a different project.
      projectId: process.env.EAS_PROJECT_ID ?? 'af01c72e-2764-4cfe-833c-df306dc4d3cd',
    },
  },
  owner: process.env.EAS_OWNER ?? 'develop.imran',
};

export default config;
