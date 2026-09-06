import { fireEvent, render, screen } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import { SearchSelect } from './SearchSelect';

const OPTIONS = [
  { value: 'kube-coder', label: 'kube-coder' },
  { value: 'pool', label: 'Pool Hall' },
  { value: 'smush', label: 'smush', hint: 'git' },
];

function open(name = 'Project') {
  fireEvent.click(screen.getByLabelText(name));
  return screen.getByLabelText(`Search ${name}`);
}

function optionText(name = 'Project') {
  return [...screen.getByRole('listbox', { name }).querySelectorAll('[role="option"]')].map(
    (o) => o.textContent,
  );
}

describe('SearchSelect', () => {
  it('shows the selected label, not the raw value', () => {
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="pool" onChange={() => {}} />);
    expect(screen.getByLabelText('Project').textContent).toContain('Pool Hall');
  });

  it('filters as you type, over label and value', () => {
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />);
    const search = open();

    fireEvent.input(search, { target: { value: 'hall' } });   // matches the label
    expect(optionText()).toEqual(['Pool Hall']);

    fireEvent.input(search, { target: { value: 'kube' } });    // matches the value
    expect(optionText()).toEqual(['kube-coder']);
  });

  it('clearing the filter brings everything back', () => {
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />);
    const search = open();
    fireEvent.input(search, { target: { value: 'zzz' } });
    expect(optionText()).toEqual([]);
    fireEvent.input(search, { target: { value: '' } });
    expect(optionText()).toHaveLength(3);
  });

  it('is navigable from the keyboard alone', () => {
    const onChange = vi.fn();
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={onChange} />);
    const search = open();

    fireEvent.keyDown(search, { key: 'ArrowDown' });   // cursor 0 -> 1
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('pool');
  });

  it('wraps the cursor rather than dead-ending at the edges', () => {
    const onChange = vi.fn();
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={onChange} />);
    const search = open();
    fireEvent.keyDown(search, { key: 'ArrowUp' });     // 0 -> last
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('smush');
  });

  it('Escape closes without selecting', () => {
    const onChange = vi.fn();
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={onChange} />);
    const search = open();
    fireEvent.keyDown(search, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('reopening starts from an empty filter', () => {
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />);
    fireEvent.input(open(), { target: { value: 'pool' } });
    fireEvent.keyDown(screen.getByLabelText('Search Project'), { key: 'Escape' });
    open();
    expect(optionText()).toHaveLength(3);
  });

  it('offers the empty row only when one is asked for', () => {
    const { unmount } = render(
      <SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />,
    );
    expect(optionText.bind(null)).toBeTruthy();
    open();
    expect(optionText()).toHaveLength(3);
    unmount();

    render(
      <SearchSelect
        ariaLabel="Project"
        options={OPTIONS}
        value=""
        onChange={() => {}}
        emptyLabel="No project"
      />,
    );
    open();
    expect(optionText()[0]).toBe('No project');
  });

  it('commits a typed value only when custom values are allowed', () => {
    // The folder picker needs this: /api/workspace/dirs only lists folders
    // under the server root, so a custom workdir is never in the options.
    const onChange = vi.fn();
    const { unmount } = render(
      <SearchSelect ariaLabel="Folder" options={OPTIONS} value="" onChange={onChange} />,
    );
    fireEvent.input(open('Folder'), { target: { value: '/srv/elsewhere' } });
    fireEvent.keyDown(screen.getByLabelText('Search Folder'), { key: 'Enter' });
    expect(onChange).not.toHaveBeenCalled();
    unmount();

    render(
      <SearchSelect ariaLabel="Folder" options={OPTIONS} value="" onChange={onChange} allowCustom />,
    );
    fireEvent.input(open('Folder'), { target: { value: '/srv/elsewhere' } });
    fireEvent.keyDown(screen.getByLabelText('Search Folder'), { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('/srv/elsewhere');
  });

  it('a disabled picker cannot be opened', () => {
    render(
      <SearchSelect ariaLabel="Model" options={OPTIONS} value="" onChange={() => {}} disabled />,
    );
    fireEvent.click(screen.getByLabelText('Model'));
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('shows a hint beside the label without matching on it', () => {
    render(<SearchSelect ariaLabel="Folder" options={OPTIONS} value="" onChange={() => {}} />);
    const search = open('Folder');
    expect(optionText('Folder')[2]).toContain('git');
    // "git" is decoration, not a search key — matching it would make the
    // filter lie about why a row is showing.
    fireEvent.input(search, { target: { value: 'git' } });
    expect(optionText('Folder')).toEqual([]);
  });
});
