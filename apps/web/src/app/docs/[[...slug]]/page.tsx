import { source } from "@/lib/source";
import { notFound } from "next/navigation";
import { type Metadata } from "next";
import { getMDXComponents } from "@/mdx-components";

interface Props {
  params: Promise<{ slug?: string[] }>;
}

export default async function DocsPage({ params }: Props) {
  const { slug } = await params;
  const page = source.getPage(slug);

  if (!page) notFound();

  const MDX = page.data.body;

  return (
    <article className="prose mx-auto max-w-4xl">
      <MDX components={getMDXComponents()} />
    </article>
  );
}

export async function generateStaticParams() {
  return source.getPages().map((page) => ({
    slug: page.slugs,
  }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const page = source.getPage(slug);

  if (!page) return {};

  const path = slug?.join("/") ?? "";

  return {
    title: page.data.title,
    description: page.data.description,
    openGraph: {
      title: `${page.data.title} | LazyCloud Docs`,
      description: page.data.description,
      url: `https://lazycloud.dev/docs/${path}`,
    },
    alternates: {
      canonical: `https://lazycloud.dev/docs/${path}`,
    },
  };
}
