import { createFileRoute, notFound } from '@tanstack/react-router'
import browserCollections from 'fumadocs-mdx:collections/browser'
import { getMDXComponents } from '@/lib/mdx-components'
import { Suspense } from 'react'
import { getDocPagePath } from '@/server/functions'

export const Route = createFileRoute('/docs/')({
  component: DocsPage,
  loader: async () => {
    const data = await getDocPagePath({ data: [] })
    if (!data) throw notFound()
    await clientLoader.preload(data.path)
    return data
  },
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
