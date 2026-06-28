import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

/** Stack routes for the Tasks tab. */
export type TasksStackParams = {
  TaskList: undefined;
  TaskDetail: { id: string };
  NewTask: undefined;
};

export type TasksNav = NativeStackNavigationProp<TasksStackParams>;
