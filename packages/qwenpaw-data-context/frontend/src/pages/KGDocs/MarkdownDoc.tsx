import { Fragment, type ReactNode } from 'react';
import styles from './KGDocsPage.module.less';

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern =
    /(`+)([^`]+?)\1|!\[([^\]]*)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|__([^_]+)__|(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(<Fragment key={`t-${key++}`}>{text.slice(lastIndex, match.index)}</Fragment>);
    }
    if (match[2] != null) {
      nodes.push(<code key={`c-${key++}`}>{match[2]}</code>);
    } else if (match[4] != null) {
      nodes.push(<img key={`i-${key++}`} src={match[4]} alt={match[3] || ''} />);
    } else if (match[6] != null) {
      nodes.push(
        <a key={`a-${key++}`} href={match[6]} target="_blank" rel="noopener noreferrer">
          {match[5]}
        </a>,
      );
    } else {
      const strong = match[7] ?? match[8];
      const em = match[9] ?? match[10];
      if (strong != null) {
        nodes.push(<strong key={`s-${key++}`}>{strong}</strong>);
      } else if (em != null) {
        nodes.push(<em key={`e-${key++}`}>{em}</em>);
      }
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push(<Fragment key={`t-${key++}`}>{text.slice(lastIndex)}</Fragment>);
  }
  return nodes;
}

function headingLevel(line: string): number {
  const match = /^(#{1,6})\s+/.exec(line);
  return match ? match[1].length : 0;
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(line);
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map((cell) => cell.trim());
}

function flushParagraph(lines: string[], nodes: ReactNode[], key: { n: number }) {
  const text = lines.join('\n').trim();
  if (!text) return;
  nodes.push(<p key={`p-${key.n++}`}>{renderInline(text)}</p>);
  lines.length = 0;
}

export default function MarkdownDoc({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const nodes: ReactNode[] = [];
  const paragraph: string[] = [];
  const key = { n: 0 };
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith('```')) {
      flushParagraph(paragraph, nodes, key);
      const language = line.slice(3).trim();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith('```')) {
        code.push(lines[i]);
        i += 1;
      }
      nodes.push(
        <pre key={`pre-${key.n++}`} data-language={language || undefined}>
          <code>{code.join('\n')}</code>
        </pre>,
      );
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1\s*$/.test(line)) {
      flushParagraph(paragraph, nodes, key);
      nodes.push(<hr key={`hr-${key.n++}`} />);
      i += 1;
      continue;
    }

    const heading = headingLevel(line);
    if (heading) {
      flushParagraph(paragraph, nodes, key);
      const Tag = `h${heading}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
      nodes.push(<Tag key={`h-${key.n++}`}>{renderInline(line.replace(/^#{1,6}\s+/, ''))}</Tag>);
      i += 1;
      continue;
    }

    if (line.startsWith('>')) {
      flushParagraph(paragraph, nodes, key);
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith('>')) {
        quote.push(lines[i].replace(/^>\s?/, ''));
        i += 1;
      }
      nodes.push(
        <blockquote key={`q-${key.n++}`}>
          <MarkdownDoc source={quote.join('\n')} />
        </blockquote>,
      );
      continue;
    }

    if (i + 1 < lines.length && line.includes('|') && isTableSeparator(lines[i + 1])) {
      flushParagraph(paragraph, nodes, key);
      const header = splitTableRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      nodes.push(
        <div key={`tbl-${key.n++}`} className={styles.markdownTableWrap}>
          <table>
            <thead>
              <tr>
                {header.map((cell, index) => (
                  <th key={index}>{renderInline(cell)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {header.map((_, cellIndex) => (
                    <td key={cellIndex}>{renderInline(row[cellIndex] ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const listMatch = /^(\s*)([-*+]|\d+\.)\s+/.exec(line);
    if (listMatch) {
      flushParagraph(paragraph, nodes, key);
      const ordered = /\d+\./.test(listMatch[2]);
      const items: string[] = [];
      while (i < lines.length) {
        const item = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (!item) break;
        items.push(item[3]);
        i += 1;
      }
      const ListTag = ordered ? 'ol' : 'ul';
      nodes.push(
        <ListTag key={`l-${key.n++}`}>
          {items.map((item, index) => (
            <li key={index}>{renderInline(item)}</li>
          ))}
        </ListTag>,
      );
      continue;
    }

    if (!line.trim()) {
      flushParagraph(paragraph, nodes, key);
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
  }

  flushParagraph(paragraph, nodes, key);

  return <div className={styles.markdownDoc}>{nodes}</div>;
}
