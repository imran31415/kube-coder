/**
 * Markdown — a small, dependency-free markdown renderer for the Hypervisor chat
 * on mobile. The dashboard SPA pipes assistant prose through `marked` + DOMPurify
 * (see charts/workspace/web/src/routes/hypervisor/transcript.ts renderMarkdown);
 * React Native has no HTML, so we parse the same GFM-ish subset the agent
 * actually emits — headings, **bold**, *italic*, `code`, fenced code blocks,
 * bullet / numbered lists, blockquotes, links, and horizontal rules — into
 * native <Text>/<View> nodes so it looks native instead of showing raw `#`, `*`
 * and backtick characters.
 *
 * This is intentionally a pragmatic parser, not a spec-complete one: it covers
 * what a coding agent produces in chat, matching the web tab's `breaks: true`
 * (a single newline inside a paragraph is a line break).
 */
import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Clipboard from 'expo-clipboard';
import { colors, font, radius, space } from '../theme';

/** One block-level element parsed from the source. */
type MdBlock =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'quote'; text: string }
  | { kind: 'rule' }
  | { kind: 'para'; text: string };

const FENCE = /^\s*```/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*(?:[-*_])\s*(?:[-*_]\s*){2,}$/; // ---, ***, ___ (with optional spaces)
const BULLET = /^\s*[-*+]\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;

/** Fold raw markdown source into block-level elements. */
function parseBlocks(src: string): MdBlock[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const blocks: MdBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line — separates blocks.
    if (!line.trim()) {
      i++;
      continue;
    }

    // Fenced code block: everything up to the closing fence, verbatim.
    if (FENCE.test(line)) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++; // consume closing fence (or run off the end on an unterminated block)
      blocks.push({ kind: 'code', text: body.join('\n') });
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2].trim() });
      i++;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: 'rule' });
      i++;
      continue;
    }

    // List: consecutive bullet or ordered items (a run of one kind).
    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = ORDERED.test(line) && !BULLET.test(line);
      const items: string[] = [];
      while (i < lines.length) {
        const m = ordered ? lines[i].match(ORDERED) : lines[i].match(BULLET);
        if (!m) break;
        items.push(m[1]);
        i++;
      }
      blocks.push({ kind: 'list', ordered, items });
      continue;
    }

    // Blockquote: consecutive '>' lines.
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(lines[i].match(QUOTE)![1]);
        i++;
      }
      blocks.push({ kind: 'quote', text: body.join('\n') });
      continue;
    }

    // Paragraph: consecutive plain lines until a blank line or a block starter.
    const body: string[] = [];
    while (i < lines.length) {
      const l = lines[i];
      if (
        !l.trim() ||
        FENCE.test(l) ||
        HEADING.test(l) ||
        RULE.test(l) ||
        BULLET.test(l) ||
        ORDERED.test(l) ||
        QUOTE.test(l)
      ) {
        break;
      }
      body.push(l);
      i++;
    }
    blocks.push({ kind: 'para', text: body.join('\n') });
  }

  return blocks;
}

// Inline token patterns, in priority order. Code is first so formatting markers
// inside `code` stay literal.
const INLINE: { re: RegExp; kind: 'code' | 'bold' | 'italic' | 'strike' | 'link' }[] = [
  { re: /`([^`]+)`/, kind: 'code' },
  { re: /\*\*([^]+?)\*\*/, kind: 'bold' },
  { re: /__([^]+?)__/, kind: 'bold' },
  { re: /~~([^]+?)~~/, kind: 'strike' },
  { re: /(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])/, kind: 'italic' },
  { re: /(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])/, kind: 'italic' },
  { re: /\[([^\]]+)\]\(([^)\s]+)\)/, kind: 'link' },
];

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
    } else if (best.kind === 'link') {
      const url = best.b || '';
      nodes.push(
        <Text key={key} style={styles.link} onPress={() => void Linking.openURL(url).catch(() => {})}>
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

/** Render a block of markdown as native RN nodes. Reused for the Hypervisor
 *  chat's assistant prose (parity with the web tab's markdown). */
export function Markdown({ text, style }: { text: string; style?: object }) {
  const blocks = parseBlocks(text || '');
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
          case 'quote':
            return (
              <View key={key} style={styles.quote}>
                <Text style={styles.para}>{parseInline(b.text, key)}</Text>
              </View>
            );
          case 'list':
            return (
              <View key={key} style={styles.list}>
                {b.items.map((it, j) => (
                  <View key={j} style={styles.listItem}>
                    <Text style={styles.listMarker}>{b.ordered ? `${j + 1}.` : '•'}</Text>
                    <Text style={styles.listText}>{parseInline(it, `${key}-${j}`)}</Text>
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
  listText: { flex: 1, color: colors.text, fontSize: font.size.md, lineHeight: 22 },
});
