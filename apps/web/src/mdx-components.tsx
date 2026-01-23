import type { MDXComponents } from "mdx/types";
import defaultMdxComponents from "fumadocs-ui/mdx";
import { Callout, Note, Warning, Tip, Danger } from "@/components/shared/callout";

export function getMDXComponents(components?: MDXComponents): MDXComponents {
  return {
    ...defaultMdxComponents,
    // Keep our custom callout components
    Callout,
    Note,
    Warning,
    Tip,
    Danger,
    ...components,
  };
}
