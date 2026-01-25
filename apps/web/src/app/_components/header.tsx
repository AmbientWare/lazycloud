"use client";

import { useState } from "react";
import { StyledButton } from "@/components/shared/styled-button";
import { useUserContext } from "@/contexts/UserContext";
import Navigation from "./navigation";
import { ChevronRight, Menu, Zap } from "lucide-react";
import HeaderBar from "@/components/shared/header-bar";
import { LANDING_ROUTES, USER_HOME } from "@/lib/constants";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export default function Header() {
  const { isSignedIn } = useUserContext();
  const isLandingRoute = LANDING_ROUTES.includes(usePathname());
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const signedInAndNotLandingRoute = isSignedIn && !isLandingRoute;

  const navItems = signedInAndNotLandingRoute
    ? [
        { label: "Workspaces", href: "/workspaces" },
        { label: "Usage", href: "/usage" },
        { label: "Docs", href: "/docs" },
      ]
    : [
        { label: "Home", href: "/" },
        { label: "Pricing", href: "/pricing" },
        { label: "Docs", href: "/docs" },
      ];

  const ctaButton = !isSignedIn ? (
    <Link href="/login">
      <StyledButton variant="primary">
        <Zap size={18} className="group-hover:animate-pulse" />
        Login
      </StyledButton>
    </Link>
  ) : (
    <Link href={USER_HOME}>
      <StyledButton variant="primary">
        <Zap size={18} className="group-hover:animate-pulse" />
        Monitor Workspaces
        <ChevronRight
          size={20}
          className="transition-transform group-hover:translate-x-1"
        />
      </StyledButton>
    </Link>
  );

  return (
    <HeaderBar>
      <nav className="flex items-center gap-8">
        {/* Desktop navigation */}
        <div className="hidden md:flex items-center gap-8">
          <Navigation isLoggedIn={signedInAndNotLandingRoute} />
          <div className="flex items-center gap-4">{ctaButton}</div>
        </div>

        {/* Mobile hamburger button */}
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden size-11"
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
            <nav className="flex flex-col gap-2 mt-4">
              {navItems.map((item) => (
                <Link
                  key={item.label}
                  href={item.href}
                  onClick={() => setMobileMenuOpen(false)}
                  className="flex items-center min-h-[48px] px-4 py-3 text-base font-medium rounded-lg hover:bg-muted transition-colors"
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
  );
}
