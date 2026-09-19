import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/preact';
import { AssistantPicker } from './AssistantPicker';
import type { HypervisorAssistant } from '../api/hypervisor';

/**
 * An installed-but-unauthenticated agent (#702). The bug this replaces: the
 * server dropped the entry entirely when its provider key was missing, so the
 * option vanished with nothing on the page saying why — a correct install read
 * as a failed one. The entry is now listed, marked, and explained.
 */

const CLAUDE: HypervisorAssistant = { id: 'claude', label: 'Claude Code' };

const DSH: HypervisorAssistant = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: false,
  needs: 'DEEPSEEK_API_KEY',
};

const DSH_READY: HypervisorAssistant = {
  id: 'deepseek-harness',
  label: 'DeepSeek Harness',
  ready: true,
};

function renderPicker(assistants: HypervisorAssistant[], assistant: string) {
  return render(
    <AssistantPicker
      assistants={assistants}
      assistant={assistant}
      model=""
      effort=""
      onAssistant={() => {}}
      onModel={() => {}}
      onEffort={() => {}}
    />,
  );
}

describe('AssistantPicker — needs API key (#702)', () => {
  it('lists a not-ready assistant and marks it in the option text', () => {
    const { getByLabelText } = renderPicker([CLAUDE, DSH], 'claude');
    const select = getByLabelText('Assistant') as HTMLSelectElement;
    const dsh = [...select.options].find((o) => o.value === 'deepseek-harness');
    expect(dsh).toBeTruthy();
    expect(dsh!.textContent).toContain('needs API key');
  });

  it('explains what to do when the not-ready assistant is selected', () => {
    const { getByRole } = renderPicker([CLAUDE, DSH], 'deepseek-harness');
    const note = getByRole('alert');
    // The key is named so the user knows WHICH of several keys to save.
    expect(note.textContent).toContain('DeepSeek Harness needs an API key');
    expect(note.textContent).toContain('DEEPSEEK_API_KEY');
    const link = note.querySelector('a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('/settings/providers');
  });

  it('says nothing when the selected assistant is ready', () => {
    const { queryByRole } = renderPicker([CLAUDE, DSH_READY], 'deepseek-harness');
    expect(queryByRole('alert')).toBeNull();
  });

  it('treats a missing `ready` field as ready — an older server is not a broken one', () => {
    const { getByLabelText, queryByRole } = renderPicker([CLAUDE], 'claude');
    const select = getByLabelText('Assistant') as HTMLSelectElement;
    expect(select.options[0].textContent).not.toContain('needs API key');
    expect(queryByRole('alert')).toBeNull();
  });
});
