"use client";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { StyledTooltip } from "@/components/shared/styled-tooltip";
import {
  StyledCard,
  StyledCardContent,
  StyledCardHeader,
} from "@/components/shared/styled-card";
import { Eye, EyeOff, RefreshCw, Trash2 } from "lucide-react";
import type { ApiKey } from "@/interfaces/apiKeys";
import { useState } from "react";
import { deleteApiKey, updateApiKey } from "@/actions/apiKeys";
import { ExpandConfirmButton } from "@/components/shared/expand-confirm-button";
import { CopyButton } from "@/components/shared/copy-button";

interface ApiKeyCardProps {
  apiKey?: ApiKey;
  onUpdate?: () => void;
  allowDelete?: boolean;
  isLoading?: boolean;
}

const isApiKeyExpired = (expiresAt: string) => {
  const expirationDate = new Date(expiresAt);
  const now = new Date();
  return expirationDate.getTime() < now.getTime();
};

export function ApiKeyCard({
  apiKey,
  onUpdate,
  allowDelete = false,
  isLoading = false,
}: ApiKeyCardProps) {
  const [isVisible, setIsVisible] = useState(false);

  const maskApiKey = (key: string) => {
    const lastFour = key.slice(-8);
    return "•".repeat(24) + lastFour;
  };

  const handleUpdate = async () => {
    if (!apiKey) return;
    try {
      await updateApiKey(apiKey.id, "0");
      onUpdate?.();
    } catch (error) {
      console.error("Failed to update API key:", error);
    }
  };

  const handleDelete = async () => {
    if (!apiKey) return;
    try {
      await deleteApiKey(apiKey.id);
      onUpdate?.();
    } catch (error) {
      console.error("Failed to delete API key:", error);
    }
  };

  if (isLoading) {
    return (
      <StyledCard
        variant="minimal"
        className="border-muted gap-0 overflow-hidden py-2"
      >
        <StyledCardHeader>
          <div className="flex items-center justify-between">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-6 w-16" />
          </div>
        </StyledCardHeader>
        <StyledCardContent className="p-0">
          <Separator />
          <div className="flex flex-col gap-2 px-4 py-2">
            <div className="flex flex-col items-stretch justify-between gap-2 sm:flex-row sm:items-center">
              <Skeleton className="h-10 flex-1" />
              <div className="flex justify-end gap-1 sm:justify-start">
                <Skeleton className="h-8 w-8" />
                <Skeleton className="h-8 w-8" />
                <Skeleton className="h-8 w-8" />
              </div>
            </div>
            <Skeleton className="h-4 w-64" />
          </div>
        </StyledCardContent>
      </StyledCard>
    );
  }

  if (!apiKey) {
    return (
      <div className="bg-muted space-y-4 rounded-lg border p-6 text-center">
        <p className="text-muted-foreground">No active API key found</p>
      </div>
    );
  }

  return (
    <StyledCard
      variant="minimal"
      className="border-muted border-l-lazycloud/50 gap-0 overflow-hidden border-l-4 py-2 shadow-sm"
    >
      <StyledCardHeader>
        <div className="flex items-center justify-between">
          <div className="truncate font-medium">{apiKey.name}</div>
          <Badge
            variant={
              isApiKeyExpired(apiKey.expires_at) ? "destructive" : "outline"
            }
            className={`text-xs ${!isApiKeyExpired(apiKey.expires_at) ? "border-green-500 text-green-500" : ""}`}
          >
            {isApiKeyExpired(apiKey.expires_at) ? "Expired" : "Active"}
          </Badge>
        </div>
      </StyledCardHeader>
      <StyledCardContent className="p-0">
        <Separator />
        <div className="flex flex-col gap-2 px-4 py-2">
          <div className="flex flex-col items-stretch justify-between gap-2 sm:flex-row sm:items-center">
            <div
              className={`bg-muted flex-1 rounded-md p-2 font-mono text-sm ${isVisible ? "overflow-x-auto" : ""}`}
            >
              {isVisible ? (
                <span className="text-primary whitespace-nowrap">
                  {apiKey.value}
                </span>
              ) : (
                <span className="block truncate">
                  {maskApiKey(apiKey.value)}
                </span>
              )}
            </div>
            <div className="flex justify-end gap-1 sm:justify-start">
              <StyledTooltip content={isVisible ? "Hide API key" : "Show API key"}>
                <Button
                  variant="outline"
                  size="icon"
                  onClick={() => setIsVisible(!isVisible)}
                  className="h-8 w-8 shrink-0 cursor-pointer"
                >
                  {isVisible ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </Button>
              </StyledTooltip>
              <CopyButton text={apiKey.value} tooltipText="Copy API key" />
              <StyledTooltip content="Refresh API key">
                <div>
                  <ExpandConfirmButton
                    onConfirm={handleUpdate}
                    icon={RefreshCw}
                    color="yellow"
                    buttonClassName="text-yellow-500 hover:bg-yellow-500/10 cursor-pointer"
                  />
                </div>
              </StyledTooltip>
              {allowDelete && (
                <StyledTooltip content="Delete API key">
                  <div>
                    <ExpandConfirmButton
                      onConfirm={handleDelete}
                      icon={Trash2}
                      color="red"
                      buttonClassName="text-red-500 hover:bg-red-500/10 cursor-pointer"
                    />
                  </div>
                </StyledTooltip>
              )}
            </div>
          </div>
          <div className="text-muted-foreground flex gap-4 text-xs">
            <span>
              Last changed on {new Date(apiKey.updated_at).toLocaleDateString()}
            </span>
          </div>
        </div>
      </StyledCardContent>
    </StyledCard>
  );
}
