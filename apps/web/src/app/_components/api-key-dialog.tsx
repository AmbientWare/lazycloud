"use client";

import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { CopyButton } from "@/components/shared/copy-button";
import { StyledTooltip } from "@/components/shared/styled-tooltip";
import { ExpandConfirmButton } from "@/components/shared/expand-confirm-button";
import { KeyRound, RefreshCw } from "lucide-react";
import { getApiKeys, regenerateApiKey } from "@/actions/apiKeys";
import { toast } from "sonner";
import type { ApiKey } from "@/interfaces/apiKeys";

interface ApiKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const isApiKeyExpired = (expiresAt: string) => {
  const expirationDate = new Date(expiresAt);
  // Year 9999 means never expires
  if (expirationDate.getFullYear() >= 9999) return false;
  return expirationDate.getTime() < Date.now();
};

export function ApiKeyDialog({ open, onOpenChange }: ApiKeyDialogProps) {
  const [apiKey, setApiKey] = useState<ApiKey | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setIsLoading(true);
      setError(null);
      getApiKeys()
        .then((keys) => {
          const defaultKey = keys.find((k) => k.name === "default") ?? keys[0];
          setApiKey(defaultKey ?? null);
        })
        .catch((err) => {
          console.error("Failed to fetch API keys:", err);
          setError("Failed to load API key");
        })
        .finally(() => {
          setIsLoading(false);
        });
    }
  }, [open]);

  const maskApiKey = (key: string) => {
    const prefix = key.slice(0, 3);
    const lastFour = key.slice(-4);
    return `${prefix}${"•".repeat(20)}${lastFour}`;
  };

  const handleRegenerate = async () => {
    if (!apiKey?.id) return;
    try {
      const newKey = await regenerateApiKey(apiKey.id);
      setApiKey(newKey);
      toast.success("API key regenerated successfully");
    } catch (err) {
      console.error("Failed to regenerate API key:", err);
      toast.error("Failed to regenerate API key");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            API Key
          </DialogTitle>
          <DialogDescription>
            Use this key to authenticate with the LazyCloud CLI in CI/CD pipelines.
          </DialogDescription>
        </DialogHeader>

        <div className="mt-2 overflow-hidden">
          {isLoading ? (
            <div className="rounded-lg border p-4">
              <div className="flex items-center justify-between mb-3">
                <Skeleton className="h-5 w-24" />
                <Skeleton className="h-5 w-16" />
              </div>
              <Separator className="mb-3" />
              <Skeleton className="h-10 w-full mb-2" />
              <Skeleton className="h-4 w-48" />
            </div>
          ) : error ? (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
              <p className="text-sm text-red-500">{error}</p>
            </div>
          ) : apiKey ? (
            <div className="rounded-lg border border-l-4 border-l-emerald-500/50 overflow-hidden">
              {/* Header */}
              <div className="flex items-center justify-between px-4 py-3 bg-muted/30">
                <span className="font-medium text-sm">{apiKey.name}</span>
                <Badge
                  variant={isApiKeyExpired(apiKey.expires_at) ? "destructive" : "outline"}
                  className={`text-xs ${!isApiKeyExpired(apiKey.expires_at) ? "border-green-500/50 text-green-500" : ""}`}
                >
                  {isApiKeyExpired(apiKey.expires_at) ? "Expired" : "Active"}
                </Badge>
              </div>

              <Separator />

              {/* Key Display */}
              <div className="p-4 space-y-3">
                <div className="flex items-center gap-2">
                  <code className="flex-1 h-8 flex items-center bg-muted/50 border rounded-md px-3 font-mono text-sm text-muted-foreground">
                    {maskApiKey(apiKey.value)}
                  </code>
                  <CopyButton
                    text={apiKey.value}
                    tooltipText="Copy"
                    className="h-8 w-8 shrink-0 cursor-pointer"
                  />
                  <StyledTooltip content="Regenerate">
                    <div>
                      <ExpandConfirmButton
                        onConfirm={handleRegenerate}
                        icon={RefreshCw}
                        color="yellow"
                        buttonClassName="text-yellow-500 hover:bg-yellow-500/10"
                      />
                    </div>
                  </StyledTooltip>
                </div>

                {/* Usage hint */}
                <div className="text-xs text-muted-foreground space-y-1">
                  <p>Set these environment variables in your CI/CD:</p>
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    <code className="text-emerald-500">LAZYCLOUD_API_KEY</code>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-lg border p-6 text-center">
              <p className="text-muted-foreground text-sm">No API key found</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
