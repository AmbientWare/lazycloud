import * as React from 'react'
import {
  createFileRoute,
  Outlet,
  useLocation,
  Link,
} from '@tanstack/react-router'
import { DocsSidebar } from './-components/docsSidebar'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from '@/components/ui/sidebar'
import TextLogo from '@/components/shared/textLogo'

const titleMap: Record<string, string> = {
  docs: 'Documentation',
  init: 'Init',
  deploy: 'Deploy',
  destroy: 'Destroy',
  rollback: 'Rollback',
  dashboard: 'Dashboard',
  workspaces: 'Workspaces',
  deployments: 'Deployments',
  usage: 'Usage',
  labels: 'Compose Labels',
  service: 'Service Labels',
  scaling: 'Scaling Labels',
  volume: 'Volume Labels',
  examples: 'Examples',
  fastapi: 'FastAPI',
}

function DocsHeader() {
  const location = useLocation()
  const pathname = location.pathname
  const pathSegments = pathname.split('/').filter(Boolean)

  const breadcrumbs = pathSegments.map((segment, index) => {
    const path = `/${pathSegments.slice(0, index + 1).join('/')}`
    const isLast = index === pathSegments.length - 1
    const title =
      titleMap[segment] ?? segment.charAt(0).toUpperCase() + segment.slice(1)

    return {
      title,
      path,
      isLast,
    }
  })

  return (
    <div className="z-50 w-full">
      <div className="flex h-14 items-center justify-between rounded-2xl border border-border/60 bg-background/80 px-4 shadow-lg backdrop-blur-md">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <Breadcrumb className="min-w-0">
            <BreadcrumbList className="flex-wrap">
              {breadcrumbs.map((crumb) => (
                <React.Fragment key={crumb.path}>
                  <BreadcrumbItem className="truncate">
                    {crumb.isLast ? (
                      <BreadcrumbPage className="truncate">
                        {crumb.title}
                      </BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink asChild>
                        <Link to={crumb.path} className="truncate">
                          {crumb.title}
                        </Link>
                      </BreadcrumbLink>
                    )}
                  </BreadcrumbItem>
                  {!crumb.isLast && (
                    <BreadcrumbSeparator className="shrink-0" />
                  )}
                </React.Fragment>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
        </div>
        <div className="ml-4 flex shrink-0 items-center">
          <TextLogo />
        </div>
      </div>
    </div>
  )
}

export const Route = createFileRoute('/docs')({
  component: DocsLayout,
})

function DocsLayout() {
  return (
    <div className="min-h-screen w-full bg-transparent">
      <SidebarProvider>
        <DocsSidebar />
        <SidebarInset className="bg-transparent">
          {/* Mobile header with sidebar trigger */}
          <header className="sticky top-0 z-50 flex h-14 items-center gap-4 border-b bg-background px-4 md:hidden">
            <SidebarTrigger />
            <TextLogo />
          </header>
          <div className="flex h-full w-full flex-col overflow-hidden p-4">
            <div className="mx-auto hidden w-full max-w-5xl md:block">
              <DocsHeader />
            </div>
            <div className="mx-auto mt-4 flex w-full max-w-5xl flex-1 flex-col overflow-y-auto rounded-xl border border-border/60 bg-card/90 shadow-sm backdrop-blur-md">
              <main className="flex-1 p-6 lg:p-8">
                <Outlet />
              </main>
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </div>
  )
}
