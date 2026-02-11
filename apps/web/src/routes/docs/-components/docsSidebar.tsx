import { BookOpen, ChevronRight } from 'lucide-react'
import { useLocation, Link } from '@tanstack/react-router'

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubItem,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { CurrentYear } from '@/components/shared/CurrentYear'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { DocsSearch } from './search-button'

const data = {
  navMain: [
    {
      title: 'Getting Started',
      url: '/docs',
      items: [],
    },
    {
      title: 'CI/CD',
      url: '/docs/cicd',
      items: [],
    },
    {
      title: 'Architecture',
      url: '/docs/architecture',
      items: [
        {
          title: 'Networking',
          url: '/docs/architecture/networking',
        },
        {
          title: 'Builds',
          url: '/docs/architecture/builds',
        },
        {
          title: 'Scaling',
          url: '/docs/architecture/scaling',
        },
        {
          title: 'Volumes',
          url: '/docs/architecture/volumes',
        },
        {
          title: 'Secrets',
          url: '/docs/architecture/secrets',
        },
        {
          title: 'Resources',
          url: '/docs/architecture/resources',
        },
        {
          title: 'Security',
          url: '/docs/architecture/security',
        },
        {
          title: 'Reliability',
          url: '/docs/architecture/reliability',
        },
      ],
    },
    {
      title: 'Compose Labels',
      url: '/docs/labels',
      items: [
        {
          title: 'Service Labels',
          url: '/docs/labels/service',
        },
        {
          title: 'Scaling Labels',
          url: '/docs/labels/scaling',
        },
        {
          title: 'Volume Labels',
          url: '/docs/labels/volume',
        },
      ],
    },
    {
      title: 'CLI Commands',
      url: '/docs/init',
      items: [
        {
          title: 'Init',
          url: '/docs/init',
        },
        {
          title: 'Deploy',
          url: '/docs/deploy',
        },
        {
          title: 'Destroy',
          url: '/docs/destroy',
        },
        {
          title: 'Rollback',
          url: '/docs/rollback',
        },
        {
          title: 'Dashboard',
          url: '/docs/dashboard',
        },
        {
          title: 'Workspaces',
          url: '/docs/workspaces',
        },
        {
          title: 'Deployments',
          url: '/docs/deployments',
        },
        {
          title: 'Usage',
          url: '/docs/usage',
        },
      ],
    },
    {
      title: 'Examples',
      url: '/docs/examples',
      items: [
        {
          title: 'LLM Chatbot',
          url: '/docs/examples/llm-chatbot',
        },
        {
          title: 'Image Transformer',
          url: '/docs/examples/image-transformer',
        },
        {
          title: 'Stock Dashboard',
          url: '/docs/examples/stock-dashboard',
        },
      ],
    },
  ],
}

export function DocsSidebar() {
  const location = useLocation()
  const pathname = location.pathname
  const { setOpenMobile } = useSidebar()

  const isActive = (url: string) => pathname === url
  const isParentActive = (item: (typeof data.navMain)[0]) => {
    if (pathname === item.url) return true
    return item.items?.some((sub) => pathname === sub.url) ?? false
  }

  return (
    <Sidebar>
      <SidebarHeader>
        <div className="flex items-center gap-3 px-2 py-2">
          <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-lazycloud/10">
            <BookOpen className="size-4 text-lazycloud" />
          </div>
          <div className="flex flex-col gap-0.5 leading-none">
            <span className="font-semibold">Documentation</span>
            <span className="text-xs text-muted-foreground">LazyCloud</span>
          </div>
        </div>
        <div className="px-2 pt-2">
          <DocsSearch />
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu>
            {data.navMain.map((item) =>
              item.items?.length ? (
                <Collapsible
                  key={item.title}
                  asChild
                  defaultOpen={isParentActive(item)}
                  className="group/collapsible"
                >
                  <SidebarMenuItem>
                    <CollapsibleTrigger asChild>
                      <SidebarMenuButton
                        asChild
                        isActive={isActive(item.url)}
                        className={cn(
                          'font-medium',
                          isParentActive(item) && 'text-lazycloud',
                        )}
                      >
                        <Link
                          to={item.url}
                          onClick={() => setOpenMobile(false)}
                        >
                          {item.title}
                          <ChevronRight className="ml-auto transition-transform duration-200 group-data-[state=open]/collapsible:rotate-90" />
                        </Link>
                      </SidebarMenuButton>
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <SidebarMenuSub>
                        {item.items.map((subItem) => (
                          <SidebarMenuSubItem key={subItem.title}>
                            <SidebarMenuButton
                              asChild
                              isActive={isActive(subItem.url)}
                            >
                              <Link
                                to={subItem.url}
                                onClick={() => setOpenMobile(false)}
                                className={cn(
                                  isActive(subItem.url) &&
                                    'font-medium text-lazycloud',
                                )}
                              >
                                <span>{subItem.title}</span>
                              </Link>
                            </SidebarMenuButton>
                          </SidebarMenuSubItem>
                        ))}
                      </SidebarMenuSub>
                    </CollapsibleContent>
                  </SidebarMenuItem>
                </Collapsible>
              ) : (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild isActive={isActive(item.url)}>
                    <Link
                      to={item.url}
                      onClick={() => setOpenMobile(false)}
                      className={cn(
                        'font-medium',
                        isParentActive(item) && 'text-lazycloud',
                      )}
                    >
                      {item.title}
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ),
            )}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-t border-border/40 px-4 pb-6 pt-4">
        <div className="flex flex-col space-y-4">
          <div className="pt-1 text-xs text-muted-foreground/60">
            <p>
              © <CurrentYear /> LazyCloud
            </p>
          </div>
        </div>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
