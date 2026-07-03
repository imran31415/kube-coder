import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

/** Stack routes for the Tasks tab. */
export type TasksStackParams = {
  TaskList: undefined;
  TaskDetail: { id: string };
  NewTask: undefined;
};

export type TasksNav = NativeStackNavigationProp<TasksStackParams>;

/** Stack routes for the Apps tab. */
export type AppsStackParams = {
  AppList: undefined;
  AppView: { port: number; name: string };
};

export type AppsNav = NativeStackNavigationProp<AppsStackParams>;
