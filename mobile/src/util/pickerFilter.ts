/**
 * The filter behind the searchable pickers.
 *
 * Extracted so it can be tested directly: this app's suite is pure-logic
 * (`src/util/*.test.ts`) with no React Native render harness, and the rules
 * here — what matches, what a hint means, where the "none" row sits — are the
 * part worth pinning. The component is then a thin shell over it.
 */

export interface PickerOption {
  value: string;
  label: string;
  /** Secondary text, shown dimmed. Decoration only — never matched against. */
  hint?: string;
}

/**
 * Options to show for `query`, with the optional clear-selection row first.
 *
 * Matching is case-insensitive substring over the label AND the value, because
 * the two diverge in exactly the cases that matter: a folder's label is a
 * basename while its value is a full path, and a project's label is a display
 * name while its value is a slug. Matching only one of them makes a picker
 * that cannot find things the user can see.
 *
 * The hint is deliberately NOT matched. It carries things like "git" and
 * "3 running", so including it would return rows with no visible reason for
 * being there — a filter that appears to be lying.
 */
export function filterOptions(
  options: PickerOption[],
  query: string,
  emptyLabel?: string,
): PickerOption[] {
  const q = query.trim().toLowerCase();
  const hit = (o: PickerOption) =>
    !q || o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q);

  const rows = options.filter(hit);
  if (emptyLabel !== undefined && (!q || emptyLabel.toLowerCase().includes(q))) {
    return [{ value: '', label: emptyLabel }, ...rows];
  }
  return rows;
}

/**
 * Whether a typed value should be offered as its own answer.
 *
 * The folder picker needs it: `/api/workspace/dirs` only lists folders under
 * the server root, so a custom workdir is a real answer that will never appear
 * in the options. Offering it when it already matches a row would be a second,
 * identical-looking way to pick the same thing.
 */
export function canUseCustom(
  query: string,
  rows: PickerOption[],
  allowCustom: boolean,
): boolean {
  const q = query.trim();
  return allowCustom && q.length > 0 && !rows.some((r) => r.value === q);
}
