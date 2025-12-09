"use client";

import { useAuth } from "@workos-inc/authkit-nextjs/components";
import { signOut } from "@workos-inc/authkit-nextjs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LogOut, CreditCard, Loader2 } from "lucide-react";
import { useState, useEffect } from "react";
import { getCustomerPortalUrl } from "@/actions/customer-portal";
import { getUserSubscriptionTier } from "@/actions/users";
import { toast } from "sonner";
import Image from "next/image";

interface CustomUserButtonProps {
  showDetails?: boolean;
}

export function CustomUserButton({ showDetails = false }: CustomUserButtonProps) {
  const { user } = useAuth();
  const [isLoadingPortal, setIsLoadingPortal] = useState(false);
  const [subscriptionTier, setSubscriptionTier] = useState<string | undefined>();

  useEffect(() => {
    if (showDetails) {
      getUserSubscriptionTier().then(setSubscriptionTier);
    }
  }, [showDetails]);

  const handleSignOut = async () => {
    await signOut();
  };

  const handleCustomerPortal = async () => {
    setIsLoadingPortal(true);
    const toastId = toast.loading("Opening customer portal...");
    try {
      const result = await getCustomerPortalUrl();
      toast.success("Redirecting to customer portal...", { id: toastId });
      setTimeout(() => {
        window.location.href = result.url;
      }, 500);
    } catch (error) {
      console.error("Error opening customer portal:", error);
      toast.error("Failed to open customer portal", { id: toastId });
      setIsLoadingPortal(false);
    }
  };

  if (!user) {
    return null;
  }

  const initials =
    user.firstName && user.lastName
      ? `${user.firstName[0]}${user.lastName[0]}`
      : user.email?.[0]?.toUpperCase() ?? "U";

  const displayName = user.firstName && user.lastName
    ? `${user.firstName} ${user.lastName}`
    : user.email;

  const avatar = (
    <div className="relative flex size-8 shrink-0 overflow-hidden rounded-full">
      {user.profilePictureUrl ? (
        <Image
          src={user.profilePictureUrl}
          alt={displayName || "User"}
          width={32}
          height={32}
          className="size-full object-cover"
        />
      ) : (
        <div className="flex size-full items-center justify-center bg-muted">
          <span className="text-xs font-medium">{initials}</span>
        </div>
      )}
    </div>
  );

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="flex cursor-pointer items-center gap-3 rounded-lg p-1 transition-colors hover:bg-accent/50 focus:outline-none focus-visible:outline-none">
          {avatar}
          {showDetails && (
            <div className="flex flex-col items-start text-left">
              <span className="text-sm font-medium leading-tight">{displayName}</span>
              {subscriptionTier && (
                <span className="text-muted-foreground text-xs leading-tight">{subscriptionTier}</span>
              )}
            </div>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem onClick={handleCustomerPortal} disabled={isLoadingPortal} className="cursor-pointer">
          <CreditCard className="mr-2 size-4" />
          {isLoadingPortal ? "Loading..." : "Customer Portal"}
          {isLoadingPortal && <Loader2 className="ml-auto size-4 animate-spin" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleSignOut} variant="destructive" className="cursor-pointer">
          <LogOut className="mr-2 size-4" />
          Sign Out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

