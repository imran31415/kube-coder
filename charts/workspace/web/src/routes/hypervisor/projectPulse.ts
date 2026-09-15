import type { Project } from '../../api/projects';

/**
 * The project pulse and unseen-delta signals, as the Chat sidebar reads them
 * (#683).
 *
 * Both used to live on the AI CTO page's projects rail: the card showed
 * running/waiting counts, and a delta strip announced "since your last visit"
 * by diffing the brief against the `last_seen_at` baseline captured when you
 * clicked the project. With the rail gone, the signals move to the sidebar's
 * project group headers and their trigger moves with them — "you opened a chat
 * filed under this project" replaces "you clicked this project in the rail".
 *
 * Both are read straight off the project record the list endpoint already
 * returns, so a sidebar with twenty projects still costs one request.
 */

export interface Pulse {
  running: number;
  waiting: number;
}

/** Live build counts for a project — zeroes when the record carries no pulse
 *  (a registry written before #466, or a project with nothing running). */
export function pulseOf(project: Project | undefined | null): Pulse {
  return {
    running: project?.pulse?.running ?? 0,
    waiting: project?.pulse?.waiting ?? 0,
  };
}

/**
 * True when the project has moved since you last opened one of its chats.
 *
 * Deliberately conservative: a project that has never been stamped
 * (`last_seen_at === null`) is NOT unseen. Otherwise every project in a fresh
 * workspace would light up at once, which tells the user nothing — the dot has
 * to mean "this one, not the others".
 */
export function hasUnseenActivity(project: Project | undefined | null): boolean {
  const seen = project?.last_seen_at ?? null;
  const activity = project?.pulse?.last_activity_at ?? null;
  if (seen === null || activity === null) return false;
  return activity > seen;
}

/** Screen-reader / tooltip text for a group header's pulse. '' when quiet. */
export function pulseLabel(p: Pulse, unseen: boolean): string {
  const bits: string[] = [];
  if (p.running > 0) bits.push(`${p.running} running`);
  if (p.waiting > 0) bits.push(`${p.waiting} waiting on you`);
  if (unseen) bits.push('new activity since you were last here');
  return bits.join(' · ');
}
