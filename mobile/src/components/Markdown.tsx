/**
 * Markdown — a small, dependency-free markdown renderer for the Hypervisor chat
 * and the Docs screen (which is also what the desktop app shows). The dashboard
 * SPA pipes these through `marked` + DOMPurify; React Native has no HTML, so
 * util/markdownBlocks.ts parses the source and this renders the blocks as native
 * <Text>/<View> nodes, so it looks native instead of showing raw `#`, `*`, `|`
 * and backtick characters.
 */
import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react';
import { Linking, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import * as Clipboard from 'expo-clipboard';
import { colors, font, radius, space } from '../theme';
import { parseBlocks, tableColumnWidths, type MdBlock } from '../util/markdownBlocks';

// Inline token patterns, in priority order. Code is first so formatting markers
// inside `code` stay literal.
const INLINE: { re: RegExp; kind: 'code' | 'bold' | 'italic' | 'strike' | 'link' | 'image' }[] = [
  { re: /`([^`]+)`/, kind: 'code' },
  { re: /!\[([^\]]*)\]\(([^)\s]+)\)/, kind: 'image' },
  { re: /\*\*([^]+?)\*\*/, kind: 'bold' },
  { re: /__([^]+?)__/, kind: 'bold' },
  { re: /~~([^]+?)~~/, kind: 'strike' },
  { re: /(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])/, kind: 'italic' },
  { re: /(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])/, kind: 'italic' },
  { re: /\[([^\]]+)\]\(([^)\s]+)\)/, kind: 'link' },
];

function opener(url: string) {
  return () => void Linking.openURL(url).catch(() => {});
}

/** A caption, not the picture: the docs' images are repo-relative paths nothing
 *  serves, and the desktop CSP blocks remote images anyway. */
function ImageCaption({ alt, url }: { alt: string; url: string }) {
  const remote = /^https?:\/\//i.test(url);
  return (
    <Text style={remote ? styles.link : styles.imageCaption} onPress={remote ? opener(url) : undefined}>
      {`Image: ${alt || url}`}
    </Text>
  );
}

/** Parse inline markdown (bold/italic/code/strike/links) into styled Text nodes. */
function parseInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let rest = text;
  let guard = 0;

  while (rest && guard++ < 500) {
    // Find the earliest-matching inline token across all patterns.
    let best: { index: number; len: number; kind: string; a: string; b?: string } | null = null;
    for (const { re, kind } of INLINE) {
      const m = re.exec(rest);
      if (m && m.index >= 0 && (!best || m.index < best.index)) {
        best = { index: m.index, len: m[0].length, kind, a: m[1], b: m[2] };
      }
    }

    if (!best) {
      nodes.push(rest);
      break;
    }

    if (best.index > 0) nodes.push(rest.slice(0, best.index));
    const key = `${keyBase}-${nodes.length}`;

    if (best.kind === 'code') {
      nodes.push(
        <Text key={key} style={styles.codeInline}>
          {best.a}
        </Text>,
      );
    } else if (best.kind === 'bold') {
      nodes.push(
        <Text key={key} style={styles.bold}>
          {parseInline(best.a, key)}
        </Text>,
      );
    } else if (best.kind === 'italic') {
      nodes.push(
        <Text key={key} style={styles.italic}>
          {parseInline(best.a, key)}
        </Text>,
      );
    } else if (best.kind === 'strike') {
      nodes.push(
        <Text key={key} style={styles.strike}>
          {parseInline(best.a, key)}
        </Text>,
      );
    } else if (best.kind === 'image') {
      nodes.push(<ImageCaption key={key} alt={best.a} url={best.b || ''} />);
    } else if (best.kind === 'link') {
      nodes.push(
        <Text key={key} style={styles.link} onPress={opener(best.b || '')}>
          {parseInline(best.a, key)}
        </Text>,
      );
    }

    rest = rest.slice(best.index + best.len);
  }

  return nodes;
}

/** A fenced code block with a Copy button in its corner (issue #351) — parity
 *  with the web chat's hv-code-copy. Uses expo-clipboard like the rest of the
 *  app (FilesScreen, GitIdentityCard); "Copied" reverts after a beat. */
function CodeBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  async function copy() {
    try {
      await Clipboard.setStringAsync(text);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }
  return (
    <View style={styles.codeBlock}>
      <Pressable
        onPress={() => void copy()}
        hitSlop={6}
        accessibilityRole="button"
        accessibilityLabel="Copy code"
        style={({ pressed }) => [styles.codeCopy, pressed && { opacity: 0.6 }]}
      >
        <Text style={[styles.codeCopyText, copied && styles.codeCopyTextOn]}>
          {copied ? 'Copied' : 'Copy'}
        </Text>
      </Pressable>
      <Text style={styles.codeBlockText} selectable>
        {text}
      </Text>
    </View>
  );
}

const HEADING_STYLE = [styles_h(1), styles_h(2), styles_h(3), styles_h(4), styles_h(5), styles_h(6)];

/** GFM table. Scrolls sideways when it can't fit, and stretches to fill when
 *  it can: every row gets the same flexBasis/flexGrow, so columns stay aligned
 *  whatever the available width turns out to be. */
function Table({ block }: { block: Extract<MdBlock, { kind: 'table' }> }) {
  const widths = tableColumnWidths(block.header, block.rows);
  const minWidth = widths.reduce((sum, w) => sum + w, 0);
  const renderRow = (cells: string[], rowKey: string, isHeader: boolean, isLast: boolean) => (
    <View key={rowKey} style={[styles.tableRow, isHeader && styles.tableHeaderRow, isLast && styles.tableLastRow]}>
      {cells.map((cell, c) => (
        <View
          key={c}
          style={[
            styles.tableCell,
            { flexBasis: widths[c], flexGrow: widths[c] },
            c === cells.length - 1 && styles.tableLastCell,
          ]}
        >
          <Text style={[isHeader ? styles.tableHeaderText : styles.tableText, { textAlign: block.align[c] ?? 'left' }]}>
            {parseInline(cell, `${rowKey}-${c}`)}
          </Text>
        </View>
      ))}
    </View>
  );
  return (
    <ScrollView horizontal nestedScrollEnabled contentContainerStyle={styles.tableScroll}>
      <View style={[styles.table, { minWidth }]}>
        {renderRow(block.header, 'head', true, block.rows.length === 0)}
        {block.rows.map((row, r) => renderRow(row, `r${r}`, false, r === block.rows.length - 1))}
      </View>
    </ScrollView>
  );
}

function listMarker(block: Extract<MdBlock, { kind: 'list' }>, index: number, checked?: boolean): string {
  if (checked !== undefined) return checked ? '☑' : '☐';
  return block.ordered ? `${block.start + index}.` : '•';
}

function Blocks({ blocks, style }: { blocks: MdBlock[]; style?: object }) {
  return (
    <View style={[styles.root, style]}>
      {blocks.map((b, i) => {
        const key = `b${i}`;
        switch (b.kind) {
          case 'heading':
            return (
              <Text key={key} style={HEADING_STYLE[Math.min(b.level, 6) - 1]}>
                {parseInline(b.text, key)}
              </Text>
            );
          case 'code':
            return <CodeBlock key={key} text={b.text} />;
          case 'rule':
            return <View key={key} style={styles.rule} />;
          case 'table':
            return <Table key={key} block={b} />;
          case 'quote':
            return <Blocks key={key} blocks={b.blocks} style={styles.quote} />;
          case 'callout':
            return (
              <View key={key} style={styles.callout}>
                <Text style={styles.calloutLabel}>{b.label}</Text>
                <Blocks blocks={b.blocks} />
              </View>
            );
          case 'list':
            return (
              <View key={key} style={styles.list}>
                {b.items.map((it, j) => (
                  <View key={j} style={styles.listItem}>
                    <Text style={styles.listMarker}>{listMarker(b, j, it.checked)}</Text>
                    <Blocks blocks={it.blocks} style={styles.listBody} />
                  </View>
                ))}
              </View>
            );
          default:
            return (
              <Text key={key} style={styles.para}>
                {parseInline(b.text, key)}
              </Text>
            );
        }
      })}
    </View>
  );
}

/** Render a block of markdown as native RN nodes. `breaks` matches the option
 *  of the same name in `marked`: leave it on for chat, turn it off for
 *  hard-wrapped documents (see ParseOptions). */
export function Markdown({
  text,
  style,
  breaks = true,
}: {
  text: string;
  style?: object;
  breaks?: boolean;
}) {
  return <Blocks blocks={parseBlocks(text || '', { breaks })} style={style} />;
}

function styles_h(level: number) {
  const sizes = [font.size.xl, font.size.lg, font.size.md, font.size.md, font.size.md, font.size.md];
  return {
    color: colors.text,
    fontWeight: '700' as const,
    fontSize: sizes[level - 1],
    lineHeight: sizes[level - 1] + 6,
    marginTop: level <= 2 ? space.xs : 0,
  };
}

const styles = StyleSheet.create({
  root: { gap: space.sm },
  para: { color: colors.text, fontSize: font.size.md, lineHeight: 22 },
  bold: { fontWeight: '700' },
  italic: { fontStyle: 'italic' },
  strike: { textDecorationLine: 'line-through' },
  link: { color: colors.info, textDecorationLine: 'underline' },
  imageCaption: { color: colors.textMuted, fontStyle: 'italic' },
  codeInline: {
    fontFamily: font.mono,
    fontSize: font.size.sm,
    color: colors.text,
    backgroundColor: colors.surface2,
  },
  codeBlock: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: space.sm,
    paddingRight: 64, // keep the first lines clear of the Copy button
  },
  codeBlockText: { fontFamily: font.mono, fontSize: font.size.sm, color: colors.text, lineHeight: 19 },
  codeCopy: {
    position: 'absolute',
    top: 4,
    right: 4,
    zIndex: 1,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  codeCopyText: { color: colors.textMuted, fontSize: font.size.xs, fontWeight: '600' },
  codeCopyTextOn: { color: colors.accent },
  rule: { height: 1, backgroundColor: colors.border, marginVertical: space.xs },
  quote: {
    borderLeftWidth: 3,
    borderLeftColor: colors.borderStrong,
    paddingLeft: space.md,
  },
  list: { gap: space.xs },
  listItem: { flexDirection: 'row', gap: space.sm, alignItems: 'flex-start' },
  listMarker: { color: colors.textMuted, fontSize: font.size.md, lineHeight: 22, minWidth: 16 },
  listBody: { flex: 1 },
  callout: {
    borderLeftWidth: 3,
    borderLeftColor: colors.accent,
    backgroundColor: colors.card,
    borderRadius: radius.sm,
    padding: space.md,
    gap: space.xs,
  },
  calloutLabel: {
    color: colors.accent,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 1,
  },
  tableScroll: { flexGrow: 1 },
  table: {
    flexGrow: 1,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    overflow: 'hidden',
  },
  tableRow: { flexDirection: 'row', borderBottomWidth: 1, borderBottomColor: colors.border },
  tableLastRow: { borderBottomWidth: 0 },
  tableHeaderRow: { backgroundColor: colors.surface2 },
  tableCell: {
    flexShrink: 0,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRightWidth: 1,
    borderRightColor: colors.border,
  },
  tableLastCell: { borderRightWidth: 0 },
  tableText: { color: colors.text, fontSize: font.size.sm, lineHeight: 19 },
  tableHeaderText: { color: colors.text, fontSize: font.size.sm, lineHeight: 19, fontWeight: '700' },
});
