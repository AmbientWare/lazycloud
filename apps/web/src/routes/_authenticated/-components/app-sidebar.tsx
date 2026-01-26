import { Book, BarChart, Folder } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarHeader,
  SidebarFooter,
  useSidebar,
} from '@/components/ui/sidebar'
import TextLogo from '@/components/shared/textLogo'
import { CustomUserButton } from './custom-user-button'
import { CurrentYear } from '@/components/shared/CurrentYear'

// Menu items.
const items = [
  {
    title: 'Workspaces',
    url: '/workspaces',
    icon: Folder,
  },
  {
    title: 'Usage and Billing',
    url: '/usage',
    icon: BarChart,
  },
  {
    title: 'Docs',
    url: '/docs',
    icon: Book,
  },
]

export function AppSidebar() {
  const { setOpenMobile } = useSidebar()

  return (
    <Sidebar>
      <SidebarHeader className="flex h-16 items-center justify-center">
        <TextLogo />
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent className="px-4">
            <SidebarMenu>
              {items.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild>
                    <Link to={item.url} onClick={() => setOpenMobile(false)}>
                      <item.icon />
                      <span>{item.title}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-border/40 border-t px-4 pb-6 pt-4">
        <div className="flex flex-col space-y-4">
          <CustomUserButton showDetails />

          <div className="pt-1 text-xs text-muted-foreground/60">
            <p>
              &copy; <CurrentYear /> LazyCloud
            </p>
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  )
}
