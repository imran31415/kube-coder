/**
 * The Chat screen's settings sheet — which rows it shows, and what the header
 * button that opens it says about the current choices.
 *
 * The Agent / Mode / Project / Model / Folder pickers used to stack above the
 * composer, and the speak toggle and past-chats button sat in the header. They
 * all live behind one header button now; this module holds the visibility
 * rules they had inline, unchanged, so the move cannot quietly drop a control.
 *
 * Pure data — no react-native imports — so the node-side vitest suite can
 * assert it without a renderer.
 */

export interface ChatSettingsInput {
  /** A chat is selected (activeId set). */
  hasActiveId: boolean;
  /** The selected chat's thread record is loaded. Usually the same as
   *  hasActiveId; they differ for a moment right after a chat is created,
   *  before the thread list refreshes — and the old inline rows keyed some
   *  controls on one and some on the other, so both are kept. */
  hasThread: boolean;
  /** Assistants the workspace offers. */
  assistantCount: number;
  /** Projects in the registry. */
  projectCount: number;
  /** The open thread's project binding, if any. */
  threadProjectId?: string | null;
  /** Models the effective assistant offers. */
  modelCount: number;
  /** A new chat is being composed in CTO mode. */
  ctoComposing: boolean;
  /** Past chats in the list. */
  threadCount: number;
}

/** How the Folder row behaves: the open chat's folder (read-only), a CTO
 *  chat's "its project's folder" (read-only), or the editable picker. */
export type FolderRow = 'thread' | 'cto' | 'editable';

export interface ChatSettingsRows {
  agent: boolean;
  mode: boolean;
  project: boolean;
  model: boolean;
  folder: FolderRow;
  /** The speak-replies toggle is always offered. */
  sound: true;
  /** Past chats — only once there is a chat to list (as the header had it). */
  chats: boolean;
}

export function chatSettingsRows(s: ChatSettingsInput): ChatSettingsRows {
  return {
    // A new chat picks its assistant; an open one keeps the one it was
    // created with. One assistant ⇒ nothing to choose.
    agent: !s.hasActiveId && s.assistantCount > 1,
    // Creation-time only: an open thread shows its mode in the header subtitle.
    mode: !s.hasThread,
    // Hidden in a workspace with no projects, unless the open chat is filed.
    project: s.projectCount > 0 || (s.hasThread && !!s.threadProjectId),
    model: s.modelCount > 0,
    folder: s.hasThread ? 'thread' : s.ctoComposing ? 'cto' : 'editable',
    sound: true,
    chats: s.threadCount > 0,
  };
}

export interface ChatSettingsHint {
  model?: string;
  speakOn: boolean;
}

/** Accessibility label for the header button — says what is behind it and the
 *  two choices a user most wants to confirm without opening it. */
export function chatSettingsLabel(h: ChatSettingsHint): string {
  const parts = ['Chat settings'];
  if (h.model) parts.push(`model ${h.model}`);
  parts.push(h.speakOn ? 'speaking replies' : 'replies muted');
  return parts.join(', ');
}

/** The one visual hint on the button: a dot while replies are read aloud —
 *  the only setting that changes what the user hears without them looking. */
export function chatSettingsShowsDot(h: ChatSettingsHint): boolean {
  return h.speakOn;
}
