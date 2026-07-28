import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/preact';
import { AssistantPicker, EffortSelect, effortLabel } from './AssistantPicker';
import type { HypervisorAssistant } from '../api/hypervisor';

/**
 * The shared assistant/model/effort picker (#483, #362). What matters here is
 * the gating: every control is driven by what the SERVER says the assistant
 * supports, so a keyless deployment or a CLI with no effort knob simply never
 * renders a dead option.
 */

const CLAUDE: HypervisorAssistant = {
  id: 'claude',
  label: 'Claude Code',
  models: ['default', 'opus', 'sonnet'],
  efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
};

const ANTE: HypervisorAssistant = { id: 'ante', label: 'Ante CLI' };

const ZEN: HypervisorAssistant = {
  id: 'opencode-zen',
  label: 'OpenCode Zen',
  free: true,
  trainingDisclosure: true,
  models: ['grok-code'],
  efforts: [],
};

function noop() {}

function renderPicker(props: Partial<Parameters<typeof AssistantPicker>[0]> = {}) {
  return render(
    <AssistantPicker
      assistants={[CLAUDE, ANTE, ZEN]}
      assistant="claude"
      model="opus"
      effort="high"
      onAssistant={noop}
      onModel={noop}
      onEffort={noop}
      {...props}
    />,
  );
}

describe('AssistantPicker', () => {
  it('renders the effort control with all five canonical levels', () => {
    const { getByLabelText } = renderPicker();
    const select = getByLabelText('Reasoning effort') as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      'low',
      'medium',
      'high',
      'xhigh',
      'max',
    ]);
    expect(select.value).toBe('high');
  });

  it('labels xhigh readably rather than as one word', () => {
    expect(effortLabel('xhigh')).toBe('X-High');
    expect(effortLabel('low')).toBe('Low');
  });

  it('hides the effort control for an assistant whose CLI has no knob', () => {
    // Same rule as the model switcher: no server-declared support, no control.
    const { queryByLabelText } = renderPicker({ assistant: 'ante', model: '' });
    expect(queryByLabelText('Reasoning effort')).toBeNull();
    expect(queryByLabelText('Model')).toBeNull();
  });

  it('hides the effort control when the assistant declares an empty list', () => {
    const { queryByLabelText, getByLabelText } = renderPicker({
      assistant: 'opencode-zen',
      model: 'grok-code',
    });
    expect(queryByLabelText('Reasoning effort')).toBeNull();
    // …but it still has a model list, so that control stays.
    expect(getByLabelText('Model')).toBeTruthy();
  });

  it('offers only the assistants the server listed', () => {
    const { getByLabelText } = renderPicker();
    const select = getByLabelText('Assistant') as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      'claude',
      'ante',
      'opencode-zen',
    ]);
  });

  it('keeps a stored-but-unavailable choice visible instead of lying', () => {
    // A project can name a provider this workspace no longer has a key for.
    const { getByLabelText } = renderPicker({ assistant: 'gone-provider' });
    const select = getByLabelText('Assistant') as HTMLSelectElement;
    expect(select.value).toBe('gone-provider');
    expect([...select.options].map((o) => o.textContent)).toContain(
      'gone-provider (unavailable)',
    );
  });

  it('reports changes to each control', () => {
    const onAssistant = vi.fn();
    const onModel = vi.fn();
    const onEffort = vi.fn();
    const { getByLabelText } = renderPicker({ onAssistant, onModel, onEffort });

    fireEvent.change(getByLabelText('Reasoning effort'), {
      target: { value: 'xhigh' },
    });
    expect(onEffort).toHaveBeenCalledWith('xhigh');

    fireEvent.change(getByLabelText('Model'), { target: { value: 'sonnet' } });
    expect(onModel).toHaveBeenCalledWith('sonnet');

    fireEvent.change(getByLabelText('Assistant'), { target: { value: 'ante' } });
    expect(onAssistant).toHaveBeenCalledWith('ante');
  });

  it('shows the training disclosure only for providers that need it', () => {
    const { queryByRole } = renderPicker();
    expect(queryByRole('note')).toBeNull();
    const zen = renderPicker({ assistant: 'opencode-zen', model: 'grok-code' });
    expect(zen.getByRole('note').textContent).toContain('training');
  });

  it('can hide the assistant select for surfaces that pin it elsewhere', () => {
    const { queryByLabelText, getByLabelText } = renderPicker({
      showAssistant: false,
    });
    expect(queryByLabelText('Assistant')).toBeNull();
    expect(getByLabelText('Reasoning effort')).toBeTruthy();
  });
});

describe('EffortSelect', () => {
  it('renders nothing when there are no levels', () => {
    const { container } = render(
      <EffortSelect levels={[]} value="" onChange={noop} />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('can drop its eyebrow label for a cramped toolbar', () => {
    const { queryByText } = render(
      <EffortSelect
        levels={['low', 'high']}
        value="high"
        onChange={noop}
        showLabel={false}
      />,
    );
    expect(queryByText('Effort')).toBeNull();
  });

  it('is disabled while a turn is running', () => {
    const { getByLabelText } = render(
      <EffortSelect levels={['low', 'high']} value="high" onChange={noop} disabled />,
    );
    expect((getByLabelText('Reasoning effort') as HTMLSelectElement).disabled).toBe(
      true,
    );
  });
});
