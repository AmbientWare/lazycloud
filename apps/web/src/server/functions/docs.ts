import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import fs from 'fs'
import path from 'path'
import matter from 'gray-matter'
import { source } from '@/lib/source'

export interface DocContent {
  title: string
  description?: string
  content: string
  slug: string
}

// Server function for getting doc page path (used with fumadocs-mdx)
export const getDocPagePath = createServerFn({ method: 'GET' })
  .inputValidator(z.array(z.string()))
  .handler(async ({ data: slugs }) => {
    const page = source.getPage(slugs)
    if (!page) return null
    return { path: page.path }
  })

export const getDocContent = createServerFn({ method: 'GET' })
  .inputValidator(z.string())
  .handler(async ({ data: slug }): Promise<DocContent | null> => {
    try {
      // Build the file path
      const basePath = path.join(process.cwd(), 'src/routes/docs/-content')
      let filePath: string

      if (!slug || slug === '') {
        filePath = path.join(basePath, 'index.mdx')
      } else {
        // Try slug.mdx first, then slug/index.mdx
        const directPath = path.join(basePath, `${slug}.mdx`)
        const indexPath = path.join(basePath, slug, 'index.mdx')

        if (fs.existsSync(directPath)) {
          filePath = directPath
        } else if (fs.existsSync(indexPath)) {
          filePath = indexPath
        } else {
          return null
        }
      }

      // Read and parse the file
      const fileContent = fs.readFileSync(filePath, 'utf-8')
      const { data: frontmatter, content } = matter(fileContent)

      return {
        title: frontmatter.title || 'Documentation',
        description: frontmatter.description,
        content,
        slug,
      }
    } catch (error) {
      console.error('Error loading doc:', error)
      return null
    }
  })

export const getDocsList = createServerFn({ method: 'GET' }).handler(
  async (): Promise<{ slug: string; title: string }[]> => {
    const basePath = path.join(process.cwd(), 'src/routes/docs/-content')
    const docs: { slug: string; title: string }[] = []

    function scanDir(dir: string, prefix: string = '') {
      const files = fs.readdirSync(dir)

      for (const file of files) {
        const filePath = path.join(dir, file)
        const stat = fs.statSync(filePath)

        if (stat.isDirectory()) {
          scanDir(filePath, prefix ? `${prefix}/${file}` : file)
        } else if (file.endsWith('.mdx')) {
          const fileContent = fs.readFileSync(filePath, 'utf-8')
          const { data: frontmatter } = matter(fileContent)

          let slug = prefix
          if (file !== 'index.mdx') {
            slug = prefix
              ? `${prefix}/${file.replace('.mdx', '')}`
              : file.replace('.mdx', '')
          }

          docs.push({
            slug,
            title: frontmatter.title || file.replace('.mdx', ''),
          })
        }
      }
    }

    scanDir(basePath)
    return docs
  },
)
