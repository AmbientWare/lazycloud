"use client";

import { TerminalWindow } from "./terminal-window";

interface ComposeViewerProps {
  content: string;
}

function highlightYAML(line: string) {
  const trimmedLine = line.trim();

  // Highlight YAML keys (words followed by colon)
  const keyRegex = /^(\s*)([a-zA-Z_][\w.-]*)(:)/;
  const keyMatch = keyRegex.exec(line);

  if (keyMatch) {
    const [, indent, key, colon] = keyMatch;
    const rest = line.slice(keyMatch[0].length);

    return (
      <>
        <span className="text-muted-foreground">{indent}</span>
        <span className="font-semibold text-lazycloud">{key}</span>
        <span className="text-muted-foreground">{colon}</span>
        <span className="text-foreground">{rest}</span>
      </>
    );
  }

  // Highlight list items (lines starting with -)
  const listRegex = /^(\s*)(-)(\s+)(.+)/;
  const listMatch = listRegex.exec(line);

  if (listMatch) {
    const [, indent, dash, space, content] = listMatch;

    return (
      <>
        <span className="text-muted-foreground">{indent}</span>
        <span className="text-lazycloud">{dash}</span>
        <span>{space}</span>
        <span className="text-foreground">{content}</span>
      </>
    );
  }

  // Comments
  if (trimmedLine.startsWith("#")) {
    return <span className="text-muted-foreground/60">{line}</span>;
  }

  // Default
  return <span className="text-foreground">{line}</span>;
}

export function ComposeViewer({ content }: ComposeViewerProps) {
  const lines = content.split("\n");

  return (
    <TerminalWindow title="docker-compose.yaml" className="h-[340px]">
      <div className="flex h-full overflow-hidden bg-card/95">
        {/* Line numbers */}
        <div className="flex min-w-[2rem] select-none flex-col overflow-y-auto border-r border-border/40 bg-muted/60 py-3 pr-2 text-right font-mono text-[10px] leading-[1.6] text-muted-foreground/60">
          {lines.map((_, i) => (
            <div key={i + 1}>{i + 1}</div>
          ))}
        </div>

        {/* Code */}
        <div className="flex-1 overflow-auto bg-card/95 px-4 py-3 font-mono text-[11px] leading-[1.6]">
          <pre>
            {lines.map((line, i) => (
              <div key={i}>{highlightYAML(line)}</div>
            ))}
          </pre>
        </div>
      </div>
    </TerminalWindow>
  );
}
