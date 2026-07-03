/** Native platforms: the real react-native-webview. The .web.tsx sibling
 *  replaces this in web builds (demo/screenshots), where react-native-webview
 *  has no implementation and would crash any screen that renders it. */
export { WebView } from 'react-native-webview';
export type { WebViewProps } from 'react-native-webview';
