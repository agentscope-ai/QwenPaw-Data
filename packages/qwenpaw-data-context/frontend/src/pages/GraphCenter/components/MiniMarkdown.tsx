/** 零依赖的迷你 markdown 渲染器：覆盖探索结论用到的子集
 *  （# 标题 / **加粗** / `code` / | 表格 | / - 列表 / 1. 列表 / 段落）。
 *  不输出任何原始 HTML，文本经 React 默认转义，无注入面。 */
import type { ReactNode } from 'react';
import styles from './miniMarkdown.module.less';

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) {
      out.push(<strong key={`b${i}`}>{tok.slice(2, -2)}</strong>);
    } else {
      out.push(<code key={`c${i}`}>{tok.slice(1, -1)}</code>);
    }
    last = m.index + tok.length;
    i += 1;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const isSep = (cells: string[]) => cells.every((c) => /^:?-{2,}:?$/.test(c));
const isListItem = (s: string) => /^([-*]|\d+\.)\s+/.test(s);
const isBlockStart = (s: string) => s.startsWith('#') || s.startsWith('|') || isListItem(s);

export default function MiniMarkdown({ text }: { text: string }) {
  const lines = (text || '').split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i].trimEnd();
    const trimmed = line.trim();
    if (!trimmed || /^```/.test(trimmed)) {
      i += 1;
      continue;
    }

    // 标题
    const h = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (h) {
      blocks.push(
        <div key={key++} className={`${styles.h} ${styles[`h${h[1].length}`]}`}>
          {inline(h[2])}
        </div>,
      );
      i += 1;
      continue;
    }

    // 表格
    if (trimmed.startsWith('|')) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        const cells = lines[i]
          .trim()
          .replace(/^\|/, '')
          .replace(/\|$/, '')
          .split('|')
          .map((c) => c.trim());
        if (!isSep(cells)) rows.push(cells);
        i += 1;
      }
      if (rows.length) {
        const [head, ...body] = rows;
        blocks.push(
          <table key={key++} className={styles.table}>
            <thead>
              <tr>
                {head.map((c, j) => (
                  <th key={j}>{inline(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((r, ri) => (
                <tr key={ri}>
                  {head.map((_, ci) => (
                    <td key={ci}>{inline(r[ci] ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>,
        );
      }
      continue;
    }

    // 列表
    if (isListItem(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && isListItem(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^([-*]|\d+\.)\s+/, ''));
        i += 1;
      }
      blocks.push(
        <ul key={key++} className={styles.ul}>
          {items.map((it, j) => (
            <li key={j}>{inline(it)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // 段落：收集到空行或下一个块级元素
    const para: string[] = [trimmed];
    while (i + 1 < lines.length && lines[i + 1].trim() && !isBlockStart(lines[i + 1].trim())) {
      i += 1;
      para.push(lines[i].trim());
    }
    blocks.push(
      <p key={key++} className={styles.p}>
        {inline(para.join(' '))}
      </p>,
    );
    i += 1;
  }

  return <div className={styles.md}>{blocks}</div>;
}
