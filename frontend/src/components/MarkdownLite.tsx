import { Fragment, type ReactNode } from "react";

/**
 * Minimal Markdown for release notes: headings, bullet/numbered lists, paragraphs, fenced code,
 * **bold**, `code` and [links](https://…). It builds React elements directly — the text is never
 * interpreted as HTML — and only http(s) links are rendered as links.
 */
export function MarkdownLite({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      i++;
      blocks.push(
        <pre key={key++} className="overflow-x-auto rounded-md bg-surface px-3 py-2 font-mono text-xs">
          {code.join("\n")}
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      blocks.push(
        <p key={key++} className={level <= 2 ? "pt-1 text-base font-semibold" : "pt-1 text-sm font-semibold"}>
          {inline(h[2])}
        </p>,
      );
      i++;
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "");
        i++;
        // Continuation lines indented under the bullet belong to it.
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          item += " " + lines[i].trim();
          i++;
        }
        items.push(item);
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={key++} className={`${ordered ? "list-decimal" : "list-disc"} space-y-1 pl-5`}>
          {items.map((it, j) => (
            <li key={j}>{inline(it)}</li>
          ))}
        </List>,
      );
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(
        <p key={key++} className="border-l-2 border-line pl-3 text-muted">
          {inline(quote.join(" "))}
        </p>,
      );
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>|\s*([-*]|\d+\.)\s+)/.test(lines[i])) para.push(lines[i++]);
    blocks.push(<p key={key++}>{inline(para.join(" "))}</p>);
  }
  return <div className="space-y-2 text-sm leading-relaxed">{blocks}</div>;
}

function inline(s: string): ReactNode {
  // Tokenise code first (its contents are literal), then links and bold.
  const out: ReactNode[] = [];
  const re = /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push(
        <code key={k++} className="rounded bg-surface px-1 py-0.5 font-mono text-[0.85em]">
          {m[1]}
        </code>,
      );
    } else if (m[2] !== undefined) {
      const href = m[3];
      out.push(
        /^https?:\/\//i.test(href) ? (
          <a key={k++} href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline">
            {m[2]}
          </a>
        ) : (
          <Fragment key={k++}>{m[2]}</Fragment>
        ),
      );
    } else if (m[4] !== undefined) {
      out.push(<strong key={k++}>{m[4]}</strong>);
    }
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
