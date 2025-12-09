"use client";

import { useState } from "react";
import { StyledButton } from "@/components/shared/styled-button";
import { useUserContext } from "@/contexts/UserContext";
import Navigation from "./navigation";
import { ChevronRight, Zap } from "lucide-react";
import HeaderBar from "@/components/shared/header-bar";
import { LANDING_ROUTES, USER_HOME } from "@/lib/constants";
import { usePathname } from "next/navigation";
import Link from "next/link";
import RequestAccessDialog from "./request-access-dialot";

export default function Header() {
  const { isSignedIn } = useUserContext();
  const isLandingRoute = LANDING_ROUTES.includes(usePathname());
  const [dialogOpen, setDialogOpen] = useState(false);
  const isProduction = process.env.NODE_ENV === "production";

  const signedInAndNotLandingRoute = isSignedIn && !isLandingRoute;

  return (
    <HeaderBar>
      <nav className="flex items-center gap-8">
        <Navigation isLoggedIn={signedInAndNotLandingRoute} />
        <div className="flex items-center gap-4">
          {!isSignedIn ? (
            isProduction ? (
              <StyledButton
                variant="primary"
                onClick={() => setDialogOpen(true)}
              >
                <Zap size={18} className="group-hover:animate-pulse" />
                Deploy Now
              </StyledButton>
            ) : (
              <Link href="/login">
                <StyledButton variant="primary">
                  <Zap size={18} className="group-hover:animate-pulse" />
                  Deploy Now
                </StyledButton>
              </Link>
            )
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
      <RequestAccessDialog open={dialogOpen} onOpenChange={setDialogOpen} />
    </HeaderBar>
  );
}
