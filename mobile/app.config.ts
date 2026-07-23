import type { ExpoConfig } from 'expo/config';

/**
 * Expo app config. Drives `eas build --profile <profile>` and store submission.
 *
 * Before the first cloud build, run `eas init` (or `eas build`) once while
 * logged in — it creates the EAS project and writes `extra.eas.projectId`
 * here. Set EAS_PROJECT_ID in the environment to pin it in CI.
 *
 * Transport security (Finding 6, July 2026 review): cleartext HTTP is gated to
 * DEVELOPMENT / PREVIEW builds only. eas.json sets EXPO_PUBLIC_ALLOW_CLEARTEXT=1
 * for the `development` and `preview` profiles and leaves it unset for
 * `production`, so:
 *   - production store builds ship with iOS NSAllowsArbitraryLoads=false and
 *     Android usesCleartextTraffic=false (HTTPS only, at the OS layer);
 *   - dev/preview builds keep the blanket cleartext allowance so a self-hosted
 *     or local (minikube / kubectl port-forward) HTTP workspace is reachable.
 * The runtime host policy in src/util/urlPolicy.ts enforces the same split for
 * the credentialed requests themselves (admin token never over HTTP; workspace
 * HTTP only for loopback when cleartext is permitted).
 */

/**
 * Whether this build allows cleartext HTTP. Kept in sync with
 * isCleartextAllowed() in src/util/urlPolicy.ts — both read the same env var.
 */
export function allowCleartext(): boolean {
  const v = (process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT ?? '').trim().toLowerCase();
  return v === '1' || v === 'true' || v === 'yes';
}

export function buildConfig(): ExpoConfig {
  const cleartext = allowCleartext();

  // iOS App Transport Security. In production we do NOT open a blanket
  // NSAllowsArbitraryLoads; NSAllowsLocalNetworking (+ an explicit localhost
  // exception) still permits a loopback dev workspace over HTTP without exposing
  // every arbitrary Wi-Fi/internet host to cleartext.
  const iosNSAppTransportSecurity = cleartext
    ? {
        // dev/preview: keep the broad allowance for self-hosted / LAN HTTP hosts.
        NSAllowsArbitraryLoads: true,
        NSAllowsLocalNetworking: true,
      }
    : {
        // production: HTTPS only, with a scoped loopback exception.
        NSAllowsArbitraryLoads: false,
        NSAllowsLocalNetworking: true,
        NSExceptionDomains: {
          localhost: {
            NSExceptionAllowsInsecureHTTPLoads: true,
            NSIncludesSubdomains: false,
          },
        },
      };

  return {
    name: 'kube-coder',
    slug: 'kube-coder-mobile',
    version: '1.2',
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
        // are HTTPS (cert-manager); a self-hosted / local (minikube,
        // port-forwarded) workspace is plain HTTP on a loopback address — allowed
        // only in dev/preview (see cleartext gate above). Hosts are
        // operator-supplied, never hardcoded.
        NSAppTransportSecurity: iosNSAppTransportSecurity,
      },
    },
    android: {
      package: 'app.kubecoder.mobile',
      versionCode: 1,
      // Cleartext HTTP is enabled via the expo-build-properties plugin below
      // (same rationale + gate as the iOS ATS exception above).
      adaptiveIcon: {
        foregroundImage: './assets/android-icon-foreground.png',
        backgroundColor: '#ededf0',
      },
    },
    web: {
      favicon: './assets/favicon.png',
      bundler: 'metro',
    },
    plugins: [
      'expo-secure-store',
      // Inline video playback for the Hypervisor chat's video previews.
      'expo-video',
      [
        // Push-to-talk voice input for the Hypervisor chat (issue #396) —
        // records audio that the workspace transcribes server-side.
        'expo-audio',
        {
          microphonePermission:
            'Allow kube-coder to record a voice message to dictate to your workspace.',
        },
      ],
      [
        // Allow plain-HTTP requests on Android — ONLY in dev/preview builds
        // (self-hosted / localhost minikube workspaces). Production store builds
        // set usesCleartextTraffic=false so the token can't leave over HTTP.
        'expo-build-properties',
        { android: { usesCleartextTraffic: cleartext } },
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
}

export default buildConfig;
