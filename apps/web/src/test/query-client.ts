import { QueryClient, type QueryClientConfig } from "@tanstack/react-query";
import { onTestFinished } from "vitest";

export function testQueryClient(config: QueryClientConfig = {}): QueryClient {
  const client = new QueryClient({
    ...config,
    defaultOptions: {
      ...config.defaultOptions,
      queries: { retry: false, ...config.defaultOptions?.queries },
      mutations: { retry: false, ...config.defaultOptions?.mutations },
    },
  });
  onTestFinished(() => client.clear());
  return client;
}
