"use client";

import * as React from "react";
import { useAuth } from "@workos-inc/authkit-nextjs/components";
import { Skeleton } from "@/components/ui/skeleton";
import { CustomUserButton } from "@/app/_components/custom-user-button";

interface UserContextType {
  isSignedIn: boolean;
  isLoaded: boolean;
  UserButton: React.ReactNode;
}

const UserContext = React.createContext<UserContextType | null>(null);

export function UserProvider({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const isSignedIn = !!user;
  const isLoaded = !loading;

  const userButton = React.useMemo(() => {
    if (loading) {
      return <Skeleton className="h-10 w-10 rounded-full" />;
    }
    if (!user) {
      return null;
    }
    return <CustomUserButton />;
  }, [loading, user]);

  const value = React.useMemo(
    () => ({
      isSignedIn,
      isLoaded,
      UserButton: userButton,
    }),
    [isSignedIn, isLoaded, userButton],
  );

  return <UserContext.Provider value={value}>{children}</UserContext.Provider>;
}

export function useUserContext() {
  const context = React.useContext(UserContext);
  if (!context) {
    throw new Error("useUserContext must be used within a UserProvider");
  }
  return context;
}
