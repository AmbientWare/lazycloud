import { useState } from 'react'
import { Link, useLocation } from '@tanstack/react-router'
import { StyledButton } from '@/components/shared/styled-button'
import Navigation from './navigation'
import { ArrowUpRight, ChevronRight, Menu, Zap } from 'lucide-react'
import HeaderBar from '@/components/shared/header-bar'
import { LANDING_ROUTES, USER_HOME } from '@/lib/constants'
import { Button } from '@/components/ui/button'
import { useRouteUser } from '@/hooks/useRouteUser'
import { useIsMobile } from '@/hooks/use-mobile'
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'

function AuthButtons({ isMobile }: { isMobile: boolean }) {
  if (isMobile) {
    return (
      <div className="flex flex-col gap-2">
        <Link
          to="/login"
          className="flex items-center justify-center rounded-full border border-border/50 bg-background/50 px-4 py-2 text-sm font-medium text-muted-foreground backdrop-blur-sm transition-colors hover:text-foreground"
        >
          Log In
        </Link>
        <Link
          to="/signup"
          className="group/signup relative flex items-center justify-center gap-1.5 overflow-hidden rounded-full border border-lazycloud/30 bg-background/50 py-2 pl-4 pr-3 text-sm font-medium text-lazycloud backdrop-blur-sm transition-colors duration-300 hover:text-black"
        >
          <div className="absolute inset-0 origin-right scale-x-0 bg-lazycloud transition-transform duration-300 ease-out group-hover/signup:scale-x-100" />
          <span className="relative z-10">Sign Up</span>
          <span className="relative z-10 flex size-5 items-center justify-center rounded-full bg-lazycloud transition-colors duration-300 group-hover/signup:bg-black">
            <ArrowUpRight className="size-3 text-black transition-colors duration-300 group-hover/signup:text-lazycloud" />
          </span>
        </Link>
      </div>
    )
  }

  return (
    <div className="flex items-center rounded-full border border-border/50 bg-background/50 backdrop-blur-sm">
      {/* Log In link */}
      <Link
        to="/login"
        className="px-4 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
      >
        Log In
      </Link>

      {/* Divider */}
      <div className="h-4 w-px bg-border/50" />

      {/* Sign Up link with arrow - has its own hover group */}
      <Link
        to="/signup"
        className="group/signup relative flex items-center gap-1.5 overflow-hidden rounded-r-full py-2 pl-3 pr-2 text-sm font-medium text-lazycloud transition-colors duration-300 hover:text-black"
      >
        {/* Expanding background on hover - only on signup */}
        <div className="absolute inset-0 origin-right scale-x-0 bg-lazycloud transition-transform duration-300 ease-out group-hover/signup:scale-x-100" />
        <span className="relative z-10">Sign Up</span>
        <span className="relative z-10 flex size-5 items-center justify-center rounded-full bg-lazycloud transition-colors duration-300 group-hover/signup:bg-black">
          <ArrowUpRight className="size-3 text-black transition-colors duration-300 group-hover/signup:text-lazycloud" />
        </span>
      </Link>
    </div>
  )
}

export default function Header() {
  const user = useRouteUser()
  const isSignedIn = !!user
  const location = useLocation()
  const isLandingRoute = LANDING_ROUTES.includes(location.pathname)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const isMobile = useIsMobile()

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

  const ctaButtons = !isSignedIn ? (
    <AuthButtons isMobile={isMobile} />
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
          <div className="flex items-center gap-4">{ctaButtons}</div>
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
              <div className="mt-4 px-4">{ctaButtons}</div>
            </nav>
          </SheetContent>
        </Sheet>
      </nav>
    </HeaderBar>
  )
}
