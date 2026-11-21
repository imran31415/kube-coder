import { describe, expect, it } from 'vitest';
import { chatSettingsLabel, chatSettingsRows, chatSettingsShowsDot, type ChatSettingsInput } from './chatSettings';

const NEW_CHAT: ChatSettingsInput = {
  hasActiveId: false,
  hasThread: false,
  assistantCount: 2,
  projectCount: 3,
  threadProjectId: null,
  modelCount: 2,
  ctoComposing: false,
  threadCount: 4,
};

describe('chatSettingsRows', () => {
  it('offers every control for a new chat in a full workspace', () => {
    expect(chatSettingsRows(NEW_CHAT)).toEqual({
      agent: true,
      mode: true,
      project: true,
      model: true,
      folder: 'editable',
      sound: true,
      chats: true,
    });
  });

  it('hides the agent picker when there is only one assistant', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, assistantCount: 1 }).agent).toBe(false);
  });

  it('drops the creation-time pickers once a chat is open', () => {
    const rows = chatSettingsRows({ ...NEW_CHAT, hasActiveId: true, hasThread: true });
    expect(rows.agent).toBe(false);
    expect(rows.mode).toBe(false);
    // The folder stays visible, read-only, so the user can see which repo
    // the open chat targets (#636).
    expect(rows.folder).toBe('thread');
    // The model live-switches an open thread (#308).
    expect(rows.model).toBe(true);
  });

  it('keeps the agent picker off while a just-created chat has no record yet', () => {
    const rows = chatSettingsRows({ ...NEW_CHAT, hasActiveId: true, hasThread: false });
    expect(rows.agent).toBe(false);
    // Mode and Folder key on the thread record, exactly as the inline rows did.
    expect(rows.mode).toBe(true);
    expect(rows.folder).toBe('editable');
  });

  it('shows a CTO chat its project folder instead of the picker', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, ctoComposing: true }).folder).toBe('cto');
  });

  it('hides the project picker in a workspace with no projects', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, projectCount: 0 }).project).toBe(false);
  });

  it("still shows an open chat's project binding with no registry loaded", () => {
    expect(
      chatSettingsRows({ ...NEW_CHAT, hasActiveId: true, hasThread: true, projectCount: 0, threadProjectId: 'p1' }).project,
    ).toBe(true);
  });

  it('hides the model picker when the assistant offers no choice', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, modelCount: 0 }).model).toBe(false);
  });

  it('lists past chats only once there are some', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, threadCount: 0 }).chats).toBe(false);
  });

  it('always offers the sound toggle', () => {
    expect(chatSettingsRows({ ...NEW_CHAT, threadCount: 0, modelCount: 0 }).sound).toBe(true);
  });
});

describe('chatSettingsLabel', () => {
  it('names the model and speech state', () => {
    expect(chatSettingsLabel({ model: 'opus', speakOn: true })).toBe(
      'Chat settings, model opus, speaking replies',
    );
  });

  it('omits the model when there is none', () => {
    expect(chatSettingsLabel({ speakOn: false })).toBe('Chat settings, replies muted');
  });
});

describe('chatSettingsShowsDot', () => {
  it('marks the button only while replies are spoken', () => {
    expect(chatSettingsShowsDot({ speakOn: true })).toBe(true);
    expect(chatSettingsShowsDot({ speakOn: false, model: 'opus' })).toBe(false);
  });
});
