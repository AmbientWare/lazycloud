import { useEffect, useState } from "react";
import { MDXRemote } from "next-mdx-remote";
import { serialize } from "next-mdx-remote/serialize";
import type { MDXRemoteSerializeResult } from "next-mdx-remote";
import { useMDXComponents } from "./mdx-components";
import Loading from "@/components/shared/loadingComponent";

interface RemoteMarkdownProps {
  path: string;
}

const repoUrl = "https://github.com/AmbientWare/lazycloud-docs";
const branch = "main";

export default function RemoteMarkdown({ path }: RemoteMarkdownProps) {
  const [mdxSource, setMdxSource] = useState<MDXRemoteSerializeResult | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const components = useMDXComponents({});

  useEffect(() => {
    const fetchContent = async () => {
      try {
        // Convert GitHub URL to raw content URL
        const rawUrl =
          repoUrl
            .replace("github.com", "raw.githubusercontent.com")
            .replace(/\/$/, "") + `/${branch}/${path}`;

        const response = await fetch(rawUrl);
        if (!response.ok) {
          throw new Error(`Failed to fetch content: ${response.statusText}`);
        }
        const text = await response.text();

        // Serialize the MDX content
        const mdxSource = await serialize(text, {
          mdxOptions: {
            development: process.env.NODE_ENV === "development",
          },
        });

        setMdxSource(mdxSource);
      } catch (err) {
        const errorMessage =
          err instanceof Error ? err.message : "Failed to load content";
        setError(errorMessage);
      }
    };

    void fetchContent();
  }, [path]);

  if (error) {
    return <div className="text-red-500">Error: {error}</div>;
  }

  if (!mdxSource) {
    return <Loading />;
  }

  return mdxSource ? (
    <div className="prose dark:prose-invert max-w-none">
      <MDXRemote {...mdxSource} components={components} />
    </div>
  ) : (
    <Loading />
  );
}
