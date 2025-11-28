"use client";

import { useUser, useClerk } from "@clerk/nextjs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LogOut, CreditCard, Loader2 } from "lucide-react";
import { useState } from "react";
import { getCustomerPortalUrl } from "@/actions/customer-portal";
import { toast } from "sonner";
import Image from "next/image";

export function CustomUserButton() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const [isLoadingPortal, setIsLoadingPortal] = useState(false);

  const handleSignOut = () => {
    void signOut({ redirectUrl: "/" });
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
      : user.emailAddresses[0]?.emailAddress[0]?.toUpperCase() ?? "U";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <div className="bg-secondary/50 hover:bg-secondary flex cursor-pointer items-center gap-3 rounded-md p-2 transition-colors">
          <div className="relative flex size-8 shrink-0 overflow-hidden rounded-full">
            {user.imageUrl ? (
              <Image
                src={user.imageUrl}
                alt={user.fullName ?? "User"}
                width={32}
                height={32}
                className="aspect-square size-full object-cover"
              />
            ) : (
              <div className="flex size-full items-center justify-center rounded-full bg-muted">
                <span className="text-xs font-medium">{initials}</span>
              </div>
            )}
          </div>
          <div className="flex flex-col justify-center">
            <span className="text-sm font-medium">Account</span>
            <span className="text-muted-foreground text-xs">
              Manage profile
            </span>
          </div>
        </div>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem onClick={handleCustomerPortal} disabled={isLoadingPortal}>
          <CreditCard className="mr-2 size-4" />
          {isLoadingPortal ? "Loading..." : "Customer Portal"}
          {isLoadingPortal && <Loader2 className="ml-auto size-4 animate-spin" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleSignOut} variant="destructive">
          <LogOut className="mr-2 size-4" />
          Sign Out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

