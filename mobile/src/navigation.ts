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
  /** `startPort` opens the start form pre-filled on that app — the Apps tab's
   *  Scan button lands here. The start form lives on this screen, so the port
   *  belongs on this route: the `NewScan` and `ConnectScanner` routes that
   *  used to be declared here were never registered in `SecurityStack`, so
   *  the Scan button could only switch tabs and drop the app it was
   *  pressed on. */
  ScanList: { startPort?: number } | undefined;
  ScanDetail: { id: string };
};

export type SecurityNav = NativeStackNavigationProp<SecurityStackParams>;
