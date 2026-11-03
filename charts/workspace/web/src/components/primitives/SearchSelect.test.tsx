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

  it('stays open while its own option list scrolls', () => {
    render(<SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />);
    open();
    // The popover listens for scrolls in the capture phase so it can close when
    // the page moves out from under its fixed position. That also hears the
    // list's own scroll — which used to slam it shut the moment you tried to
    // reach an option below the fold, leaving typing as the only way in.
    fireEvent.scroll(screen.getByRole('listbox', { name: 'Project' }));
    expect(screen.queryByRole('listbox', { name: 'Project' })).not.toBeNull();
  });

  it('still closes when something outside it scrolls', () => {
    render(
      <div>
        <div data-testid="page" />
        <SearchSelect ariaLabel="Project" options={OPTIONS} value="" onChange={() => {}} />
      </div>,
    );
    open();
    fireEvent.scroll(screen.getByTestId('page'));
    expect(screen.queryByRole('listbox', { name: 'Project' })).toBeNull();
  });
});

describe('SearchSelect — keeping the highlight on screen (#814)', () => {
  /** Ten projects: more than the fake 90px list viewport can show at once. */
  const MANY = Array.from({ length: 10 }, (_, i) => ({
    value: `p${i + 1}`,
    label: `project-${i + 1}`,
  }));
  const ROW_H = 30;
  const PAD = 4; // .ss-list top padding

  /** happy-dom does no layout: give the open list a 90px viewport of 30px
   *  rows, and a scrollTop that sticks. Row offsets are read live, so rows a
   *  filter keeps (same element, new index) report where they now sit. */
  function fakeListLayout(): HTMLElement {
    const list = screen.getByRole('listbox', { name: 'Project' });
    let top = 0;
    Object.defineProperty(list, 'clientHeight', { configurable: true, get: () => 90 });
    Object.defineProperty(list, 'scrollTop', {
      configurable: true,
      get: () => top,
      set: (v: number) => {
        top = v;
      },
    });
    for (const row of [...list.children]) {
      Object.defineProperty(row, 'offsetTop', {
        configurable: true,
        get: () => PAD + [...list.children].indexOf(row) * ROW_H,
      });
      Object.defineProperty(row, 'offsetHeight', { configurable: true, get: () => ROW_H });
    }
    return list;
  }

  /** Whether the highlighted row lies wholly inside the list's viewport. */
  function cursorInView(list: HTMLElement): boolean {
    const row = list.querySelector('.ss-opt-cursor') as HTMLElement;
    return (
      row.offsetTop >= list.scrollTop &&
      row.offsetTop + row.offsetHeight <= list.scrollTop + list.clientHeight
    );
  }

  it('scrolls the list to follow the arrow keys past the fold', () => {
    render(<SearchSelect ariaLabel="Project" options={MANY} value="" onChange={() => {}} />);
    const search = open();
    const list = fakeListLayout();

    for (let i = 0; i < 6; i++) fireEvent.keyDown(search, { key: 'ArrowDown' });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-7');
    expect(list.scrollTop).toBeGreaterThan(0);
    expect(cursorInView(list)).toBe(true);

    // Wrapping from the first row up to the last follows it to the bottom too.
    for (let i = 0; i < 7; i++) fireEvent.keyDown(search, { key: 'ArrowUp' });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-10');
    expect(cursorInView(list)).toBe(true);
  });

  it('brings the first match back into view when a filter is typed after scrolling', () => {
    render(<SearchSelect ariaLabel="Project" options={MANY} value="" onChange={() => {}} />);
    const search = open();
    const list = fakeListLayout();
    list.scrollTop = 200; // the reader wheeled down the list
    fireEvent.scroll(list);

    // "project-1" and "project-10" match; the first is the row Enter picks.
    fireEvent.input(search, { target: { value: 'project-1' } });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-1');
    expect(list.scrollTop).toBe(0);
  });

  it('does not scroll the list when the pointer highlights a row', () => {
    render(<SearchSelect ariaLabel="Project" options={MANY} value="" onChange={() => {}} />);
    open();
    const list = fakeListLayout();

    // Row 3 is cut off by the 90px viewport. Following the pointer would slide
    // a new row under it, highlight that, and scroll again.
    fireEvent.mouseMove(list.children[2], { clientX: 40, clientY: 80 });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-3');
    expect(list.scrollTop).toBe(0);
  });

  it('keeps the keyboard highlight when rows slide under a resting pointer', () => {
    render(<SearchSelect ariaLabel="Project" options={MANY} value="" onChange={() => {}} />);
    const search = open();
    const list = fakeListLayout();

    fireEvent.mouseMove(list.children[1], { clientX: 40, clientY: 50 }); // the reader points
    fireEvent.keyDown(search, { key: 'ArrowDown' });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-3');

    // The browser re-fires hover on whatever row is now under the still pointer
    // (the list scrolled, or a filter re-rendered it). Same coordinates: the
    // reader did not move, so the keyboard keeps the highlight.
    fireEvent.mouseMove(list.children[5], { clientX: 40, clientY: 50 });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-3');

    // A real move still points.
    fireEvent.mouseMove(list.children[5], { clientX: 40, clientY: 52 });
    expect(list.querySelector('.ss-opt-cursor')?.textContent).toBe('project-6');
  });
});
