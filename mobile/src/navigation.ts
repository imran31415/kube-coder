import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

/** Stack routes for the Tasks tab. */
export type TasksStackParams = {
  TaskList: undefined;
  // `tab: 'changes'` opens an isolated Build's worktree sheet straight away —
  // the link a Board review card follows (#701).
  TaskDetail: { id: string; tab?: 'changes' };
  NewTask: undefined;
};

export type TasksNav = NativeStackNavigationProp<TasksStackParams>;

/** Stack routes for the Apps tab. */
export type AppsStackParams = {
  AppList: undefined;
  AppView: { port: number; name: string };
};

export type AppsNav = NativeStackNavigationProp<AppsStackParams>;

/** Stack routes for the Docs tab (#250). The list passes the manifest title
 *  through so the article header is right before the body has loaded. */
export type DocsStackParams = {
  DocsList: undefined;
  DocsArticle: { id: string; title: string };
};

export type DocsNav = NativeStackNavigationProp<DocsStackParams>;

/** Stack routes for the Security tab (#726). A finding is reached inside the
 *  scan rather than as its own route, because it only means anything in the
 *  context of the scan that produced it. */
export type SecurityStackParams = {
  ScanList: undefined;
  /** `startPort` opens the start sheet pre-filled — the Apps tab's Scan
   *  button and a push both land here. */
  ScanDetail: { id: string };
  NewScan: { startPort?: number } | undefined;
  ConnectScanner: undefined;
};

export type SecurityNav = NativeStackNavigationProp<SecurityStackParams>;
