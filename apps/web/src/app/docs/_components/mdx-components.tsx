import type { MDXComponents } from "mdx/types";
import { cn } from "@/lib/utils";
import type { ClassValue } from "clsx";
import Link from "next/link";

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
          "text-foreground mt-10 scroll-m-20 border-b pb-2 text-3xl font-semibold tracking-tight first:mt-0",
          className,
        )}
        {...props}
      />
    ),
    h3: ({ className, ...props }: { className?: ClassValue }) => (
      <h3
        className={cn(
          "text-foreground mt-8 scroll-m-20 text-2xl font-semibold tracking-tight",
          className,
        )}
        {...props}
      />
    ),
    h4: ({ className, ...props }: { className?: ClassValue }) => (
      <h4
        className={cn(
          "text-foreground mt-8 scroll-m-20 text-xl font-semibold tracking-tight",
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
          "border-primary/20 text-muted-foreground mt-6 border-l-2 pl-6 italic",
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
      <div className="bg-card my-6 w-full overflow-y-auto rounded-lg border">
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
    pre: ({ className, ...props }: { className?: ClassValue }) => (
      <pre
        className={cn(
          "bg-card mt-6 mb-4 overflow-x-auto rounded-lg border p-4 text-sm shadow-sm",
          className,
        )}
        {...props}
      />
    ),
    code: ({ className, ...props }: { className?: ClassValue }) => (
      <code
        className={cn(
          "bg-muted text-foreground relative rounded px-[0.3rem] py-[0.2rem] font-mono text-sm",
          className,
        )}
        {...props}
      />
    ),
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

      // Handle Markdown-style links [text](url)
      // If children is a string and contains markdown-style link, extract the text and href
      let linkText = children;
      let linkHref = href;

      if (
        typeof children === "string" &&
        children.includes("](") &&
        children.includes(")")
      ) {
        const regex = /\[(.*?)\]\((.*?)\)/;
        const match = regex.exec(children);
        if (match) {
          linkText = match[1];
          linkHref = match[2];
          // Update isExternal check with the new href
          const isExternalLink = linkHref?.startsWith("http");
          const LinkComponent = isExternalLink ? "a" : Link;
          return (
            <LinkComponent
              href={linkHref ?? "#"}
              className={cn(
                "text-primary hover:text-primary/80 font-medium underline underline-offset-4",
                className,
              )}
              {...props}
            >
              {linkText}
            </LinkComponent>
          );
        }
      }

      return (
        <Component
          href={linkHref ?? "#"}
          className={cn(
            "text-primary hover:text-primary/80 font-medium underline underline-offset-4",
            className,
          )}
          {...props}
        >
          {linkText}
        </Component>
      );
    },
    ...components,
  };
}
