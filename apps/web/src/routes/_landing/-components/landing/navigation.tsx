import { Link } from '@tanstack/react-router'
import {
  NavigationMenu,
  NavigationMenuList,
  NavigationMenuItem,
  navigationMenuTriggerStyle,
} from '@/components/ui/navigation-menu'

interface NavigationProps {
  isLoggedIn: boolean
}

export default function Navigation(props: NavigationProps) {
  const { isLoggedIn } = props

  const notLoggedInItems = [
    {
      label: 'Home',
      href: '/',
    },
    {
      label: 'Pricing',
      href: '/pricing',
    },
    {
      label: 'Docs',
      href: '/docs',
    },
  ]

  const loggedInItems = [
    {
      label: 'Workspaces',
      href: '/workspaces',
    },
    {
      label: 'Usage',
      href: '/usage',
    },
    {
      label: 'Docs',
      href: '/docs',
    },
  ]

  const items = isLoggedIn ? loggedInItems : notLoggedInItems

  return (
    <NavigationMenu>
      <NavigationMenuList className="gap-4">
        {items.map((item) => (
          <NavigationMenuItem key={item.label}>
            <Link to={item.href} className={navigationMenuTriggerStyle()}>
              {item.label}
            </Link>
          </NavigationMenuItem>
        ))}
      </NavigationMenuList>
    </NavigationMenu>
  )
}
