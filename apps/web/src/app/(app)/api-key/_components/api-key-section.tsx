"use client";

import { useEffect, useState } from "react";
import { ApiKeyCard } from "./apiKeyCard";
import type { ApiKey } from "@/interfaces/apiKeys";

interface ApiKeySectionProps {
  initialApiKey?: ApiKey;
  onUpdate?: () => void;
  isLoading?: boolean;
}

export function ApiKeySection({ initialApiKey, onUpdate, isLoading = false }: ApiKeySectionProps) {
  const [apiKey, setApiKey] = useState(initialApiKey);

  // Sync when prop changes
  useEffect(() => {
    setApiKey(initialApiKey);
  }, [initialApiKey]);

  return (
    <ApiKeyCard
      apiKey={apiKey}
      onUpdate={onUpdate}
      allowDelete={false}
      isLoading={isLoading}
    />
  );
}
