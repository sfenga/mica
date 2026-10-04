import { esc } from "./ui.js";
import { resolveMention } from "./state.js";

export const MENTION_PATTERN =
  /\[([^\]\n]*)\]\(mention:\/\/(agent|squad)\/([\w.-]+)\)|@(agent|squad):([\w.-]+)/g;
export function mentionMarkup(kind, id, label) {
  const target = resolveMention(kind, id);
  return `<span class="mention-chip ${kind}${target ? "" : " unknown"}" title="${esc(`${kind}:${id}`)}">@${esc(target?.name || label || id)}</span>`;
}
function inline(source) {
  // Parse source tokens, never reparse generated HTML. Raw HTML stays escaped.
  const token =
    /`([^`\n]+)`|\[([^\]\n]+)\]\(([^\s)]+)\)|@(agent|squad):([\w.-]+)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
  let html = "",
    end = 0;
  for (const match of source.matchAll(token)) {
    html += esc(source.slice(end, match.index));
    if (match[1] !== undefined) html += `<code>${esc(match[1])}</code>`;
    else if (match[2] !== undefined) {
      const mention = /^mention:\/\/(agent|squad)\/([\w.-]+)$/.exec(match[3]);
      if (mention)
        html += mentionMarkup(
          mention[1],
          mention[2],
          match[2].replace(/^@/, ""),
        );
      else if (/^(https?:\/\/|mailto:)/i.test(match[3]))
        html += `<a href="${esc(match[3])}" target="_blank" rel="noopener noreferrer">${esc(match[2])}</a>`;
      else html += esc(match[0]);
    } else if (match[4]) html += mentionMarkup(match[4], match[5]);
    else if (match[6]) html += `<strong>${esc(match[6])}</strong>`;
    else html += `<em>${esc(match[7])}</em>`;
    end = match.index + match[0].length;
  }
  return html + esc(source.slice(end));
}
export function markdown(source = "") {
  const lines = String(source).replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^\s*```/.test(line)) {
      const language = line.replace(/^\s*```/, "").trim();
      const code = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++)
        code.push(lines[i]);
      i++;
      blocks.push(
        `<div class="code-block"><div class="code-language">${esc(language || "代码")}</div><pre><code>${esc(code.join("\n"))}</code></pre></div>`,
      );
    } else if (/^#{1,6}\s/.test(line)) {
      const [, hashes, text] = /^(#{1,6})\s+(.*)$/.exec(line);
      const level = Math.min(hashes.length + 1, 6);
      blocks.push(`<h${level}>${inline(text)}</h${level}>`);
      i++;
    } else if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i]))
        quote.push(inline(lines[i++].replace(/^>\s?/, "")));
      blocks.push(`<blockquote>${quote.join("<br>")}</blockquote>`);
    } else if (/^\s*(?:[-*]|\d+\.)\s/.test(line)) {
      const ordered = /^\s*\d+\./.test(line),
        tag = ordered ? "ol" : "ul",
        items = [];
      const pattern = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
      while (i < lines.length && pattern.test(lines[i]))
        items.push(`<li>${inline(pattern.exec(lines[i++])[1])}</li>`);
      blocks.push(`<${tag}>${items.join("")}</${tag}>`);
    } else {
      const paragraph = [inline(line)];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^\s*(?:```|#{1,6}\s|>\s?|[-*]\s|\d+\.\s)/.test(lines[i])
      )
        paragraph.push(inline(lines[i++]));
      blocks.push(`<p>${paragraph.join("<br>")}</p>`);
    }
  }
  return blocks.join("");
}
