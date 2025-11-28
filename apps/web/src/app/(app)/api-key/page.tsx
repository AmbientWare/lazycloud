"use client";

import { useState, useEffect } from "react";
import { Terminal } from "lucide-react";
import { getApiKeys } from "@/actions/apiKeys";
import { ApiKeySection } from "./_components/api-key-section";
import { Spinner } from "@/components/shared/spinner";
import { CopyButton } from "@/components/shared/copy-button";
import { StyledCard, StyledCardContent } from "@/components/shared/styled-card";
import { SectionDivider } from "@/components/shared/section-divider";
import { SectionHeader } from "@/components/shared/section-header";
import type { ApiKey } from "@/interfaces/apiKeys";

export default function ApiKeyPage() {
  const [apiKey, setApiKey] = useState<ApiKey | undefined>(undefined);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const loadApiKey = async () => {
      try {
        const apiKeys = await getApiKeys();
        // Get the first non-expired key, or any key with year 9999 (never expires), or just the first key
        const activeKey =
          apiKeys.find((key) => {
            const expiresAt = new Date(key.expires_at);
            return (
              expiresAt.getFullYear() === 9999 || expiresAt.getTime() > Date.now()
            );
          }) ?? apiKeys[0];
        setApiKey(activeKey);
      } catch (error) {
        console.error("Failed to load API key:", error);
      } finally {
        setIsLoading(false);
      }
    };

    void loadApiKey();
  }, []);

  const handleUpdate = async () => {
    setIsLoading(true);
    try {
      const apiKeys = await getApiKeys();
      const activeKey =
        apiKeys.find((key) => {
          const expiresAt = new Date(key.expires_at);
          return (
            expiresAt.getFullYear() === 9999 || expiresAt.getTime() > Date.now()
          );
        }) ?? apiKeys[0];
      setApiKey(activeKey);
    } catch (error) {
      console.error("Failed to refresh API key:", error);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <>
      <div className="pb-2">
        <h1 className="mb-2 flex items-center gap-3 text-3xl font-bold tracking-tight">
          <Terminal className="text-lazycloud h-7 w-7" />
          API Key
        </h1>
        <p className="text-muted-foreground mt-1.5 text-sm">
          Manage your API key for CLI authentication
        </p>
      </div>

      <SectionDivider spacing="md">
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            {isLoading && apiKey && <Spinner size="sm" />}
            <SectionHeader
              title="Your API Key"
              description="Keep your API key secret. Use it to authenticate the LazyCloud CLI."
              titleSize="lg"
            />
          </div>
          <ApiKeySection initialApiKey={apiKey} onUpdate={handleUpdate} isLoading={isLoading && !apiKey} />
        </div>
      </SectionDivider>

      <section className="space-y-4">
        <SectionDivider spacing="lg">
          <SectionHeader
            title="1. Installation"
            description="Install the LazyCloud CLI"
            titleSize="lg"
          />
          <StyledCard
            variant="minimal"
            className="border-muted bg-muted mt-4 gap-0 overflow-hidden py-2"
          >
            <StyledCardContent className="p-0">
              <div className="flex items-center justify-between gap-2 px-4 py-2.5">
                <code className="text-foreground flex-1 font-mono text-sm">
                  pip install lazycloud
                </code>
                <CopyButton text="pip install lazycloud" />
              </div>
            </StyledCardContent>
          </StyledCard>
        </SectionDivider>
      </section>

      <section className="space-y-4">
        <SectionDivider spacing="md">
          <SectionHeader
            title="2. Authentication"
            description="Login with your API key"
            titleSize="lg"
          />
          <div className="mt-4 space-y-2">
            <StyledCard
              variant="minimal"
              className="border-muted bg-muted gap-0 overflow-hidden py-2"
            >
              <StyledCardContent className="p-0">
                <div className="flex items-center justify-between gap-2 px-4 py-2.5">
                  <code className="text-foreground flex-1 font-mono text-sm">
                    lazycloud login
                  </code>
                  <CopyButton text="lazycloud login" />
                </div>
              </StyledCardContent>
            </StyledCard>
            <p className="text-muted-foreground pl-1 text-xs">
              You'll be prompted for your API key
            </p>
          </div>
        </SectionDivider>
      </section>
    </>
  );
}
