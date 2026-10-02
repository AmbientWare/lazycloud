import { Server, Users } from "lucide-react";
import { useSyncExternalStore } from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { FleetSettings } from "./FleetSettings";
import { UsersSettings } from "./UsersSettings";

const sections = [
  { value: "users", label: "Users", icon: Users },
  { value: "fleet", label: "Fleet", icon: Server },
] as const;

const desktopQuery = "(min-width: 640px)";

function subscribeViewport(listener: () => void) {
  const media = window.matchMedia(desktopQuery);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}

function isDesktop() {
  return window.matchMedia(desktopQuery).matches;
}

export function AdminSettings() {
  const desktop = useSyncExternalStore(subscribeViewport, isDesktop, () => false);
  return (
    <Tabs
      defaultValue="users"
      orientation={desktop ? "vertical" : "horizontal"}
      className="flex min-h-0 flex-1 flex-col gap-3 sm:flex-row"
    >
      <TabsList
        aria-label="Administration"
        className="shrink-0 justify-start sm:h-auto sm:w-32 sm:flex-col sm:items-stretch sm:border-0"
      >
        {sections.map(({ value, label, icon: Icon }) => (
          <TabsTrigger
            key={value}
            value={value}
            className="flex items-center justify-start gap-2 sm:mb-0 sm:h-9 sm:border-b-0 sm:border-l-2 sm:data-[state=active]:bg-muted/50"
          >
            <Icon className="hidden size-3.5 sm:block" aria-hidden="true" />
            {label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="users" className="flex min-h-0 min-w-0 flex-1 flex-col">
        <UsersSettings />
      </TabsContent>
      <TabsContent value="fleet" className="flex min-h-0 min-w-0 flex-1 flex-col">
        <FleetSettings />
      </TabsContent>
    </Tabs>
  );
}
