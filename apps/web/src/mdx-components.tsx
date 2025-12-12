import type { MDXComponents } from "mdx/types";
import { cn } from "@/lib/utils";
import type { ClassValue } from "clsx";
import Link from "next/link";
import { CodeBlock } from "@/components/shared/code-block";
import { Callout, Note, Warning, Tip, Danger } from "@/components/shared/callout";

export function useMDXComponents(components: MDXComponents): MDXComponents {
  return {
    h1: ({ className, ...props }: { className?: ClassValue }) => (
      <h1
        className={cn(
          "text-foreground mt-2 scroll-m-20 text-4xl font-bold tracking-tight",
          className,
        )}
        {...props}
      />
    ),
    h2: ({ className, ...props }: { className?: ClassValue }) => (
      <h2
        className={cn(
          "text-foreground mt-10 scroll-m-20 text-2xl font-semibold tracking-tight first:mt-0",
          className,
        )}
        {...props}
      />
    ),
    h3: ({ className, ...props }: { className?: ClassValue }) => (
      <h3
        className={cn(
          "text-foreground mt-8 scroll-m-20 text-xl font-semibold tracking-tight",
          className,
        )}
        {...props}
      />
    ),
    h4: ({ className, ...props }: { className?: ClassValue }) => (
      <h4
        className={cn(
          "text-foreground mt-6 scroll-m-20 text-lg font-semibold tracking-tight",
          className,
        )}
        {...props}
      />
    ),
    p: ({ className, ...props }: { className?: ClassValue }) => (
      <p
        className={cn(
          "text-muted-foreground leading-7 [&:not(:first-child)]:mt-6",
          className,
        )}
        {...props}
      />
    ),
    ul: ({ className, ...props }: { className?: ClassValue }) => (
      <ul
        className={cn("text-muted-foreground my-6 ml-6 list-disc", className)}
        {...props}
      />
    ),
    ol: ({ className, ...props }: { className?: ClassValue }) => (
      <ol
        className={cn(
          "text-muted-foreground my-6 ml-6 list-decimal",
          className,
        )}
        {...props}
      />
    ),
    li: ({ className, ...props }: { className?: ClassValue }) => (
      <li className={cn("mt-2", className)} {...props} />
    ),
    blockquote: ({ className, ...props }: { className?: ClassValue }) => (
      <blockquote
        className={cn(
          "border-lazycloud/50 bg-lazycloud/5 text-muted-foreground mt-6 rounded-r-lg border-l-4 py-3 pl-4 pr-4",
          className,
        )}
        {...props}
      />
    ),
    img: ({
      className,
      alt,
      ...props
    }: React.ImgHTMLAttributes<HTMLImageElement> & {
      className?: ClassValue;
    }) => (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        className={cn(
          "bg-card rounded-lg border shadow-sm transition-colors",
          className,
        )}
        alt={alt}
        {...props}
      />
    ),
    hr: ({ ...props }) => (
      <hr className="border-muted-foreground/20 my-8" {...props} />
    ),
    table: ({ className, ...props }: { className?: ClassValue }) => (
      <div className="bg-card/90 backdrop-blur-md border-border/60 my-6 w-full overflow-y-auto rounded-lg border shadow-sm shadow-black/30 dark:shadow-white/10">
        <table className={cn("w-full", className)} {...props} />
      </div>
    ),
    tr: ({ className, ...props }: { className?: ClassValue }) => (
      <tr
        className={cn("even:bg-muted/50 m-0 border-t p-0", className)}
        {...props}
      />
    ),
    th: ({ className, ...props }: { className?: ClassValue }) => (
      <th
        className={cn(
          "text-foreground border px-4 py-2 text-left font-bold [&[align=center]]:text-center [&[align=right]]:text-right",
          className,
        )}
        {...props}
      />
    ),
    td: ({ className, ...props }: { className?: ClassValue }) => (
      <td
        className={cn(
          "text-muted-foreground border px-4 py-2 text-left [&[align=center]]:text-center [&[align=right]]:text-right",
          className,
        )}
        {...props}
      />
    ),
    pre: ({
      className,
      children,
      ...props
    }: {
      className?: ClassValue;
      children?: React.ReactNode;
    }) => (
      <CodeBlock className={className} {...props}>
        {children}
      </CodeBlock>
    ),
    code: ({ className, ...props }: { className?: ClassValue }) => {
      // Check if code is inside a pre block (has language class)
      const isCodeBlock =
        typeof className === "string" && className.includes("language-");

      if (isCodeBlock) {
        return (
          <code className={cn("text-foreground font-mono", className)} {...props} />
        );
      }

      // Inline code styling
      return (
        <code
          className={cn(
            "bg-muted text-foreground rounded px-1.5 py-0.5 font-mono text-sm",
            className,
          )}
          {...props}
        />
      );
    },
    a: ({
      className,
      href,
      children,
      ...props
    }: {
      className?: ClassValue;
      href?: string;
      children?: React.ReactNode;
    }) => {
      const isExternal = href?.startsWith("http");
      const Component = isExternal ? "a" : Link;

      return (
        <Component
          href={href ?? "#"}
          className={cn(
            "text-primary hover:text-primary/80 font-medium underline underline-offset-4",
            className,
          )}
          {...(isExternal ? { target: "_blank", rel: "noopener noreferrer" } : {})}
          {...props}
        >
          {children}
        </Component>
      );
    },
    // Callout components for notes, warnings, tips
    Callout,
    Note,
    Warning,
    Tip,
    Danger,
    ...components,
  };
}
