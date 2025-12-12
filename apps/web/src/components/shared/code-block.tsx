"use client";

import { cn } from "@/lib/utils";
import { CopyButton } from "./copy-button";
import type { ReactNode } from "react";
import type { ClassValue } from "clsx";

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

export function CodeBlock({ children, className }: CodeBlockProps) {
  const text = extractText(children).trim();
  const isSingleLine = !text.includes("\n");

  return (
    <div className="group relative mt-6 mb-4">
      <pre
        className={cn(
          "bg-card/90 backdrop-blur-md border-border/60 overflow-x-auto rounded-lg border p-4 pr-12 text-sm shadow-sm shadow-black/30 dark:shadow-white/10",
          className,
        )}
      >
        {children}
      </pre>
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
