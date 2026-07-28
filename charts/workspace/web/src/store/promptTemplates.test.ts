import { beforeEach, describe, expect, it } from 'vitest';
import {
  deleteTemplate,
  MAX_TEMPLATES,
  promptTemplates,
  saveTemplate,
  suggestTemplateName,
} from './promptTemplates';

beforeEach(() => {
  localStorage.clear();
  promptTemplates.value = [];
});

describe('promptTemplates', () => {
  it('saves a template and persists it to localStorage', async () => {
    const tpl = saveTemplate('  Run tests  ', '  npm test  ');
    expect(tpl).toMatchObject({ name: 'Run tests', prompt: 'npm test' });
    expect(promptTemplates.value).toHaveLength(1);
    // The persistence effect runs on the signal's microtask flush.
    await Promise.resolve();
    const raw = JSON.parse(localStorage.getItem('kc.prompt.templates.v1')!);
    expect(raw[0]).toMatchObject({ name: 'Run tests', prompt: 'npm test' });
  });

  it('rejects a blank name or blank prompt', () => {
    expect(saveTemplate('', 'do a thing')).toBeNull();
    expect(saveTemplate('Name', '   ')).toBeNull();
    expect(promptTemplates.value).toHaveLength(0);
  });

  it('updates in place when the same name is saved again', () => {
    saveTemplate('Deploy', 'old prompt');
    const id = promptTemplates.value[0].id;
    saveTemplate('deploy', 'new prompt');
    expect(promptTemplates.value).toHaveLength(1);
    expect(promptTemplates.value[0]).toMatchObject({ id, name: 'deploy', prompt: 'new prompt' });
  });

  it('keeps newest first and caps the list', () => {
    for (let i = 0; i < MAX_TEMPLATES + 5; i++) saveTemplate(`t${i}`, `p${i}`);
    expect(promptTemplates.value).toHaveLength(MAX_TEMPLATES);
    expect(promptTemplates.value[0].name).toBe(`t${MAX_TEMPLATES + 4}`);
  });

  it('deletes by id', () => {
    saveTemplate('a', 'pa');
    saveTemplate('b', 'pb');
    deleteTemplate(promptTemplates.value[0].id);
    expect(promptTemplates.value.map((t) => t.name)).toEqual(['a']);
  });

  it('suggests a name from the prompt first line', () => {
    expect(suggestTemplateName('Fix the login bug\nand add a test')).toBe('Fix the login bug');
    expect(suggestTemplateName('   ')).toBe('Template');
    expect(suggestTemplateName('x'.repeat(80))).toHaveLength(41); // 40 chars + ellipsis
  });
});
