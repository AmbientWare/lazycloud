import { createFileRoute, notFound, Link } from '@tanstack/react-router'
import browserCollections from 'fumadocs-mdx:collections/browser'
import { getMDXComponents } from '@/lib/mdx-components'
import { Suspense } from 'react'
import { getDocPagePath } from '@/server/functions'

export const Route = createFileRoute('/docs/$')({
  component: DocsPage,
  loader: async ({ params }) => {
    const slugs = params._splat?.split('/').filter(Boolean) ?? []
    const data = await getDocPagePath({ data: slugs })
    if (!data) throw notFound()
    await clientLoader.preload(data.path)
    return data
  },
  notFoundComponent: () => (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <h1 className="mb-4 text-2xl font-bold">Page Not Found</h1>
      <p className="mb-6 text-muted-foreground">
        The documentation page you're looking for doesn't exist.
      </p>
      <Link to="/docs" className="text-primary hover:underline">
        Go back to documentation
      </Link>
    </div>
  ),
})

const clientLoader = browserCollections.docs.createClientLoader({
  component({ default: MDX }) {
    return (
      <article className="prose prose-zinc dark:prose-invert mx-auto max-w-4xl">
        <MDX components={getMDXComponents()} />
      </article>
    )
  },
})

function DocsPage() {
  const data = Route.useLoaderData()
  return (
    <Suspense fallback={<div>Loading...</div>}>
      {clientLoader.useContent(data.path)}
    </Suspense>
  )
}
