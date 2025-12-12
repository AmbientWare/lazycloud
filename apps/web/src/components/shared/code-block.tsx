"use client";

import { cn } from "@/lib/utils";
import { CopyButton } from "./copy-button";
import { type ReactNode, useState, useEffect, useMemo } from "react";
import type { ClassValue } from "clsx";
import { codeToHtml } from "shiki";

interface CodeBlockProps {
  children: ReactNode;
  className?: ClassValue;
}

function extractText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (!node) return "";

  if (Array.isArray(node)) {
    return node.map(extractText).join("");
  }

  if (typeof node === "object" && "props" in node) {
    const props = node.props as { children?: ReactNode };
    return extractText(props.children);
  }

  return "";
}

function extractLanguage(node: ReactNode): string | null {
  if (!node || typeof node !== "object" || !("props" in node)) return null;

  const props = node.props as { className?: string; children?: ReactNode };

  // Check if this node has a language class
  if (typeof props.className === "string") {
    const match = props.className.match(/language-(\w+)/);
    if (match?.[1]) return match[1];
  }

  // Recursively check children
  if (props.children) {
    if (Array.isArray(props.children)) {
      for (const child of props.children) {
        const lang = extractLanguage(child);
        if (lang) return lang;
      }
    } else {
      return extractLanguage(props.children);
    }
  }

  return null;
}

export function CodeBlock({ children, className }: CodeBlockProps) {
  const text = useMemo(() => extractText(children).trim(), [children]);
  const language = useMemo(() => extractLanguage(children) ?? "text", [children]);
  const isSingleLine = !text.includes("\n");

  const [highlightedHtml, setHighlightedHtml] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    codeToHtml(text, {
      lang: language,
      theme: "github-dark-default",
    })
      .then((html) => {
        if (!cancelled) {
          setHighlightedHtml(html);
        }
      })
      .catch(() => {
        // If highlighting fails, we'll just show the plain text
      });

    return () => {
      cancelled = true;
    };
  }, [text, language]);

  return (
    <div className="group relative mt-6 mb-4">
      {highlightedHtml ? (
        <div
          className={cn(
            "bg-card/90 backdrop-blur-md border-border/60 overflow-x-auto rounded-lg border shadow-sm shadow-black/30 dark:shadow-white/10",
            "[&_pre]:overflow-x-auto [&_pre]:p-4 [&_pre]:pr-12 [&_pre]:text-sm [&_pre]:leading-relaxed [&_pre]:!bg-transparent",
            "[&_code]:font-mono [&_code]:text-sm",
            className,
          )}
          dangerouslySetInnerHTML={{ __html: highlightedHtml }}
        />
      ) : (
        <pre
          className={cn(
            "bg-card/90 backdrop-blur-md border-border/60 overflow-x-auto rounded-lg border p-4 pr-12 text-sm leading-relaxed shadow-sm shadow-black/30 dark:shadow-white/10",
            className,
          )}
        >
          <code className="font-mono">{text}</code>
        </pre>
      )}
      <CopyButton
        text={text}
        variant="ghost"
        size="icon"
        className={cn(
          "absolute right-2 h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100",
          isSingleLine ? "top-1/2 -translate-y-1/2" : "top-2",
        )}
      />
    </div>
  );
}
