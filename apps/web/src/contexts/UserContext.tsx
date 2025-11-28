"use client";

import * as React from "react";
import { UserButton, useUser } from "@clerk/nextjs";
import { Skeleton } from "@/components/ui/skeleton";

interface UserContextType {
  isSignedIn: boolean;
  isLoaded: boolean;
  UserButton: React.ReactNode;
}

const UserContext = React.createContext<UserContextType | null>(null);

export function UserProvider({ children }: { children: React.ReactNode }) {
  const { isSignedIn = false, isLoaded } = useUser();

  const userButton = React.useMemo(() => {
    if (!isLoaded) {
      return <Skeleton className="h-10 w-10 rounded-full" />;
    }
    if (!isSignedIn) {
      return null;
    }
    return (
      <UserButton
        appearance={{
          elements: {
            avatarBox: "w-10 h-10",
          },
        }}
      />
    );
  }, [isLoaded, isSignedIn]);

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
