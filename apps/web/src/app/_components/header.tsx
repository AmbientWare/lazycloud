"use client";

import { StyledButton } from "@/components/shared/styled-button";
import { useUserContext } from "@/contexts/UserContext";
import Navigation from "./navigation";
import { ChevronRight, Zap } from "lucide-react";
import HeaderBar from "@/components/shared/header-bar";
import { LANDING_ROUTES, USER_HOME } from "@/lib/constants";
import { usePathname } from "next/navigation";
import Link from "next/link";

export default function Header() {
  const { isSignedIn } = useUserContext();
  const isLandingRoute = LANDING_ROUTES.includes(usePathname());

  const signedInAndNotLandingRoute = isSignedIn && !isLandingRoute;

  return (
    <HeaderBar>
      <nav className="flex items-center gap-8">
        <Navigation isLoggedIn={signedInAndNotLandingRoute} />
        <div className="flex items-center gap-4">
          {!isSignedIn ? (
            <Link href="/request-access">
              <StyledButton variant="primary">
                <Zap size={18} className="group-hover:animate-pulse" />
                Request Access
              </StyledButton>
            </Link>
          ) : (
            <Link href={USER_HOME}>
              <StyledButton variant="primary">
                <Zap size={18} className="group-hover:animate-pulse" />
                Monitore Workspaces
                <ChevronRight
                  size={20}
                  className="transition-transform group-hover:translate-x-1"
                />
              </StyledButton>
            </Link>
          )}
        </div>
      </nav>
    </HeaderBar>
  );
}
