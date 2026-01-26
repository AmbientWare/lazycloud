import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'
import { Link } from '@tanstack/react-router'

interface MarkdownRendererProps {
  content: string
  className?: string
}

export function MarkdownRenderer({
  content,
  className,
}: MarkdownRendererProps) {
  return (
    <div className={cn('prose prose-invert prose-docs max-w-none', className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ className, ...props }) => (
            <h1
              className={cn(
                'mb-6 scroll-m-20 text-4xl font-bold tracking-tight',
                className,
              )}
              {...props}
            />
          ),
          h2: ({ className, ...props }) => (
            <h2
              className={cn(
                'mb-4 mt-10 scroll-m-20 border-b border-border pb-2 text-2xl font-semibold tracking-tight first:mt-0',
                className,
              )}
              {...props}
            />
          ),
          h3: ({ className, ...props }) => (
            <h3
              className={cn(
                'mb-3 mt-8 scroll-m-20 text-xl font-semibold tracking-tight',
                className,
              )}
              {...props}
            />
          ),
          h4: ({ className, ...props }) => (
            <h4
              className={cn(
                'mb-2 mt-6 scroll-m-20 text-lg font-semibold tracking-tight',
                className,
              )}
              {...props}
            />
          ),
          p: ({ className, ...props }) => (
            <p
              className={cn(
                'leading-7 text-muted-foreground [&:not(:first-child)]:mt-4',
                className,
              )}
              {...props}
            />
          ),
          ul: ({ className, ...props }) => (
            <ul
              className={cn(
                'my-4 ml-6 list-disc text-muted-foreground',
                className,
              )}
              {...props}
            />
          ),
          ol: ({ className, ...props }) => (
            <ol
              className={cn(
                'my-4 ml-6 list-decimal text-muted-foreground',
                className,
              )}
              {...props}
            />
          ),
          li: ({ className, ...props }) => (
            <li className={cn('mt-2', className)} {...props} />
          ),
          a: ({ href, className, children, ...props }) => {
            // Handle internal links
            if (href?.startsWith('/')) {
              return (
                <Link
                  to={href}
                  className={cn(
                    'font-medium text-lazycloud underline underline-offset-4 hover:text-lazycloud/80',
                    className,
                  )}
                >
                  {children}
                </Link>
              )
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className={cn(
                  'font-medium text-lazycloud underline underline-offset-4 hover:text-lazycloud/80',
                  className,
                )}
                {...props}
              >
                {children}
              </a>
            )
          },
          code: ({ className, children, ...props }) => {
            // Check if this is an inline code or a code block
            const isInline = !className?.includes('language-')

            if (isInline) {
              return (
                <code
                  className={cn(
                    'relative rounded bg-muted px-[0.3rem] py-[0.2rem] font-mono text-sm font-semibold text-foreground',
                    className,
                  )}
                  {...props}
                >
                  {children}
                </code>
              )
            }

            return (
              <code className={cn('font-mono text-sm', className)} {...props}>
                {children}
              </code>
            )
          },
          pre: ({ className, ...props }) => (
            <pre
              className={cn(
                'my-4 overflow-x-auto rounded-lg border border-border bg-muted p-4',
                className,
              )}
              {...props}
            />
          ),
          blockquote: ({ className, ...props }) => (
            <blockquote
              className={cn(
                'mt-4 border-l-4 border-lazycloud pl-4 italic text-muted-foreground',
                className,
              )}
              {...props}
            />
          ),
          table: ({ className, ...props }) => (
            <div className="my-4 w-full overflow-x-auto">
              <table
                className={cn('w-full border-collapse text-sm', className)}
                {...props}
              />
            </div>
          ),
          thead: ({ className, ...props }) => (
            <thead className={cn('bg-muted', className)} {...props} />
          ),
          th: ({ className, ...props }) => (
            <th
              className={cn(
                'border border-border px-4 py-2 text-left font-semibold',
                className,
              )}
              {...props}
            />
          ),
          td: ({ className, ...props }) => (
            <td
              className={cn('border border-border px-4 py-2', className)}
              {...props}
            />
          ),
          hr: ({ className, ...props }) => (
            <hr className={cn('my-6 border-border', className)} {...props} />
          ),
          strong: ({ className, ...props }) => (
            <strong
              className={cn('font-semibold text-foreground', className)}
              {...props}
            />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
