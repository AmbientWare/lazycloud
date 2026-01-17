"use client";

import { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";
import { StyledTooltip } from "@/components/shared/styled-tooltip";

interface CopyButtonProps {
  text: string;
  tooltipText?: string;
  timeout?: number;
  variant?:
    | "default"
    | "outline"
    | "ghost"
    | "link"
    | "destructive"
    | "secondary";
  size?: "default" | "sm" | "lg" | "icon";
  className?: string;
}

export function CopyButton({
  text,
  tooltipText = "Copy",
  timeout = 750,
  variant = "outline",
  size = "icon",
  className = "h-11 w-11 shrink-0 cursor-pointer",
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<NodeJS.Timeout | undefined>(undefined);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);

      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }

      timeoutRef.current = setTimeout(() => {
        setCopied(false);
      }, timeout);
    } catch (err) {
      console.error("Failed to copy to clipboard:", err);
    }
  };

  return (
    <StyledTooltip content={copied ? "Copied!" : tooltipText}>
      <Button
        variant={variant}
        size={size}
        onClick={handleCopy}
        className={className}
        aria-label={copied ? "Copied" : tooltipText}
      >
        {copied ? (
          <Check className="h-4 w-4 text-green-500" />
        ) : (
          <Copy className="h-4 w-4" />
        )}
      </Button>
    </StyledTooltip>
  );
}
