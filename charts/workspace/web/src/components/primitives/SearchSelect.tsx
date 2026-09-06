import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'preact/hooks';
import './SearchSelect.css';

/**
 * A select you can type into.
 *
 * A native `<select>` is fine for five fixed options and hostile for fifty:
 * the only way to reach an entry is to scroll a list you cannot filter, and on
 * mobile the equivalent was a horizontally-scrolling chip rail — every option
 * off-screen, no way to jump. Both get worse exactly as a workspace gets more
 * useful, because the lists are projects, folders and models.
 *
 * So: a button showing the current choice, and a popover with a filter box.
 * Type to narrow, arrows to move, Enter to pick, Escape to leave. Nothing is
 * hidden behind the filter — clearing it shows everything again.
 *
 * ## Why the popover is `position: fixed`
 *
 * The chat toolbar sets `overflow-x: auto` at ≤390px so the pill row can
 * scroll instead of clipping. An absolutely-positioned popover inside it is
 * then clipped by that same overflow — measured on a 390px viewport, the
 * option list was cut to the search box alone, which is the one width where
 * this component matters most. Fixed positioning is not clipped by ancestor
 * overflow, so the popover is placed from the trigger's viewport rect and
 * clamped to stay on screen.
 *
 * ## Why not a plain `<input list=…>`
 *
 * `<datalist>` is the tempting one-liner, and its filtering, styling and
 * keyboard behaviour differ per browser with no way to correct any of it. It
 * also cannot show a secondary hint per row, which is what makes the folder
 * picker readable ("(git)") and the project picker unambiguous.
 */

export interface SearchOption {
  value: string;
  label: string;
  /** Secondary text, shown dimmed after the label. Never matched against. */
  hint?: string;
}

export interface SearchSelectProps {
  options: SearchOption[];
  value: string;
  onChange: (value: string) => void;
  /** Accessible name. Required — this renders as a button, not a labelled input. */
  ariaLabel: string;
  /** Shown when `value` matches no option and no custom value is allowed. */
  placeholder?: string;
  disabled?: boolean;
  title?: string;
  class?: string;
  /** Text for the row that clears the selection. Omitted when undefined. */
  emptyLabel?: string;
  /**
   * Let the typed text be committed as-is when it matches nothing.
   *
   * The folder picker needs it: `/api/workspace/dirs` only lists folders under
   * the server root, so a custom HYPERVISOR_WORKDIR is a legitimate value that
   * will never appear in the options.
   */
  allowCustom?: boolean;
}

/** Case-insensitive substring over the label and the value. */
function matches(option: SearchOption, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    option.label.toLowerCase().includes(q) || option.value.toLowerCase().includes(q)
  );
}

export function SearchSelect({
  options,
  value,
  onChange,
  ariaLabel,
  placeholder = 'Select…',
  disabled = false,
  title,
  class: klass,
  emptyLabel,
  allowCustom = false,
}: SearchSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const [rect, setRect] = useState<{ left: number; top: number; width: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Document-global ids would collide the moment two of these render together,
  // which on the chat toolbar is always.
  const listId = useId();

  const rows = useMemo(() => {
    const filtered = options.filter((o) => matches(o, query));
    if (emptyLabel !== undefined && matches({ value: '', label: emptyLabel }, query)) {
      return [{ value: '', label: emptyLabel }, ...filtered];
    }
    return filtered;
  }, [options, query, emptyLabel]);

  const selected = options.find((o) => o.value === value);
  const buttonText =
    selected?.label ?? (value ? value : emptyLabel !== undefined ? emptyLabel : placeholder);

  const close = useCallback(() => {
    setOpen(false);
    setQuery('');
    setCursor(0);
    setRect(null);
  }, []);

  /** Place the popover under the trigger, clamped into the viewport. */
  const place = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = Math.max(r.width, 220);
    const maxLeft = Math.max(8, window.innerWidth - width - 8);
    setRect({ left: Math.min(r.left, maxLeft), top: r.bottom + 4, width });
  }, []);

  const commit = useCallback(
    (next: string) => {
      onChange(next);
      close();
    },
    [onChange, close],
  );

  // Opening focuses the filter box: the whole point is that you can start
  // typing immediately rather than hunting the list.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // A fixed popover does not travel with its trigger, so anything that moves
  // the trigger has to close it rather than leave it stranded.
  useEffect(() => {
    if (!open) return;
    const onMove = () => close();
    window.addEventListener('resize', onMove);
    window.addEventListener('scroll', onMove, true);
    return () => {
      window.removeEventListener('resize', onMove);
      window.removeEventListener('scroll', onMove, true);
    };
  }, [open, close]);

  // Close on an outside press. Pointerdown rather than click so a press that
  // starts outside and ends inside cannot leave it open.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      if (!rootRef.current?.contains(e.target as Node)) close();
    };
    document.addEventListener('pointerdown', onDown, true);
    return () => document.removeEventListener('pointerdown', onDown, true);
  }, [open, close]);

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!rows.length) return;
      const delta = e.key === 'ArrowDown' ? 1 : -1;
      setCursor((c) => (c + delta + rows.length) % rows.length);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const row = rows[cursor];
      if (row) commit(row.value);
      // A typed value that matches nothing is still a real answer for the
      // folder picker, whose options never include a custom workdir.
      else if (allowCustom && query.trim()) commit(query.trim());
    }
  }

  return (
    <div class={['ss', klass || ''].filter(Boolean).join(' ')} ref={rootRef}>
      <button
        type="button"
        class="ss-trigger"
        disabled={disabled}
        title={title}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        ref={triggerRef}
        onClick={() => {
          if (open) return close();
          place();
          setOpen(true);
        }}
      >
        <span class={`ss-value${selected || value ? '' : ' ss-value-empty'}`}>{buttonText}</span>
        <span class="ss-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div
          class="ss-pop"
          style={
            rect
              ? { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px` }
              : undefined
          }
        >
          <input
            ref={inputRef}
            class="ss-input"
            type="text"
            value={query}
            placeholder="Type to search…"
            aria-label={`Search ${ariaLabel}`}
            aria-controls={listId}
            aria-autocomplete="list"
            role="combobox"
            aria-expanded={true}
            onInput={(e) => {
              setQuery((e.target as HTMLInputElement).value);
              setCursor(0);
            }}
            onKeyDown={onKey}
          />
          <ul class="ss-list" id={listId} role="listbox" aria-label={ariaLabel}>
            {rows.map((o, i) => (
              <li
                key={o.value || '__empty__'}
                role="option"
                aria-selected={o.value === value}
                class={[
                  'ss-opt',
                  i === cursor ? 'ss-opt-cursor' : '',
                  o.value === value ? 'ss-opt-on' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                // Mouse-down, not click: a click fires after blur, by which
                // point the outside-press handler has already closed the popup.
                onMouseDown={(e) => {
                  e.preventDefault();
                  commit(o.value);
                }}
                onMouseEnter={() => setCursor(i)}
              >
                <span class="ss-opt-label">{o.label}</span>
                {o.hint && <span class="ss-opt-hint">{o.hint}</span>}
              </li>
            ))}
            {rows.length === 0 && (
              <li class="ss-empty" role="presentation">
                {allowCustom && query.trim()
                  ? `Press Enter to use “${query.trim()}”`
                  : 'No matches'}
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
