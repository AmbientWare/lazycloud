import { useState } from 'react'
import { Link, useLocation } from '@tanstack/react-router'
import { StyledButton } from '@/components/shared/styled-button'
import Navigation from './navigation'
import { ChevronRight, Menu, Zap } from 'lucide-react'
import HeaderBar from '@/components/shared/header-bar'
import { LANDING_ROUTES, USER_HOME } from '@/lib/constants'
import { Button } from '@/components/ui/button'
import { useRouteUser } from '@/hooks/useRouteUser'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'

export default function Header() {
  const user = useRouteUser()
  const isSignedIn = !!user
  const location = useLocation()
  const isLandingRoute = LANDING_ROUTES.includes(location.pathname)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  const signedInAndNotLandingRoute = isSignedIn && !isLandingRoute

  const navItems = signedInAndNotLandingRoute
    ? [
        { label: 'Workspaces', href: '/workspaces' },
        { label: 'Usage', href: '/usage' },
        { label: 'Docs', href: '/docs' },
      ]
    : [
        { label: 'Home', href: '/' },
        { label: 'Pricing', href: '/pricing' },
        { label: 'Docs', href: '/docs' },
      ]

  const ctaButton = !isSignedIn ? (
    <Link to="/login">
      <StyledButton variant="primary">
        <Zap size={18} className="group-hover:animate-pulse" />
        Login
      </StyledButton>
    </Link>
  ) : (
    <Link to={USER_HOME}>
      <StyledButton variant="primary">
        <Zap size={18} className="group-hover:animate-pulse" />
        Monitor Workspaces
        <ChevronRight
          size={20}
          className="transition-transform group-hover:translate-x-1"
        />
      </StyledButton>
    </Link>
  )

  return (
    <HeaderBar>
      <nav className="flex items-center gap-8">
        {/* Desktop navigation */}
        <div className="hidden items-center gap-8 md:flex">
          <Navigation isLoggedIn={signedInAndNotLandingRoute} />
          <div className="flex items-center gap-4">{ctaButton}</div>
        </div>

        {/* Mobile hamburger button */}
        <Button
          variant="ghost"
          size="icon"
          className="size-11 md:hidden"
          onClick={() => setMobileMenuOpen(true)}
          aria-label="Open menu"
        >
          <Menu size={24} />
        </Button>

        {/* Mobile menu sheet */}
        <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
          <SheetContent side="right" className="w-[280px]">
            <SheetHeader>
              <SheetTitle>Menu</SheetTitle>
            </SheetHeader>
            <nav className="mt-4 flex flex-col gap-2">
              {navItems.map((item) => (
                <Link
                  key={item.label}
                  to={item.href}
                  onClick={() => setMobileMenuOpen(false)}
                  className="flex min-h-[48px] items-center rounded-lg px-4 py-3 text-base font-medium transition-colors hover:bg-muted"
                >
                  {item.label}
                </Link>
              ))}
              <div className="mt-4 px-4">{ctaButton}</div>
            </nav>
          </SheetContent>
        </Sheet>
      </nav>
    </HeaderBar>
  )
}
