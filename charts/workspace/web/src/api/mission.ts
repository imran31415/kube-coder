import { apiGet } from './client';

/**
 * Client for the Mission Control queue (issue #425): one normalized feed of
 * builds, hypervisor chats and sub-agents, pre-bucketed by state so the board
 * can render columns without re-deriving status semantics per source.
 */

export type MissionKind = 'build' | 'chat' | 'subagent';
export type MissionState = 'running' | 'waiting' | 'review' | 'done';

/** One tappable choice parsed off a waiting task's screen — same shape as
 *  tasks.ts PendingPrompt/PromptOption (server parse_screen_prompt, #204). */
export interface MissionPromptOption {
  index: number | string;
  label: string;
}

export interface MissionPrompt {
  kind: 'choice' | 'yesno';
  question?: string | null;
  options: MissionPromptOption[];
}

export interface MissionOutcome {
  ok: boolean;
  detail: string;
}

/** Shallow reference to a spawned sub-agent, for the card's lineage line. */
export interface MissionChildRef {
  id: string;
  title: string;
  state: string;
}

export interface MissionCard {
  /** Namespaced id like `build:<id>` / `chat:<id>` / `subagent:<id>`. */
  id: string;
  /** Raw id for task/thread API calls (kill, follow-up, navigation). */
  ref_id: string;
  kind: MissionKind;
  state: MissionState;
  title: string;
  /** Derived one-liner of the card's current activity. */
  headline: string;
  assistant: string | null;
  model: string;
  workdir: string;
  repo: string;
  branch: string;
  created_at: number | null;
  updated_at: number | null;
  finished_at: number | null;
  waiting_since: number | null;
  waiting_prompt: MissionPrompt | null;
  outcome: MissionOutcome | null;
  parent_id: string | null;
  children: MissionChildRef[];
}

export interface MissionPulse {
  running: number;
  waiting: number;
  review: number;
  done_today: number;
  oldest_wait_s: number;
  generated_at: number;
}

export interface MissionQueue {
  cards: MissionCard[];
  pulse: MissionPulse;
}

/** Cards arrive pre-sorted: waiting → running → review → done, newest first
 *  within each group. Pure read — safe to poll. */
export const getMissionQueue = () => apiGet<MissionQueue>('/api/missioncontrol/queue');

/** One normalized activity-timeline entry in the card detail drawer. Chats
 *  derive theirs from the hypervisor activity classifier (#298); tasks from
 *  their metadata (start, sub-agent spawns, waiting, terminal transition). */
export interface MissionTimelineEntry {
  at: number | null;
  kind: 'start' | 'tool' | 'subagent' | 'waiting' | 'status' | 'error' | 'end';
  text: string;
  detail: string;
  /** Namespaced card id to cross-link (spawned sub-agent / sub-build). */
  link: string | null;
  status: 'ok' | 'error' | 'pending' | 'muted' | string;
}

export interface MissionDetail {
  card: MissionCard;
  timeline: MissionTimelineEntry[];
  /** Bounded ANSI-stripped tail of the task's output.log; '' for chats. */
  output_tail: string;
}

/** Drawer payload for one board card. 404s once a card ages off the board. */
export const getMissionCardDetail = (id: string) =>
  apiGet<MissionDetail>(`/api/missioncontrol/cards/${id}`);
