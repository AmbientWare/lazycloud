"use client";

import { Book, BarChart, Folder, Key } from "lucide-react";
import Link from "next/link";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarHeader,
  SidebarFooter,
} from "@/components/ui/sidebar";
import TextLogo from "../_components/textLogo";
import { CustomUserButton } from "./custom-user-button";
import { CurrentYear } from "@/components/shared/CurrentYear";

// Menu items.
const items = [
  {
    title: "Workspaces",
    url: "/workspaces",
    icon: Folder,
  },
  {
    title: "Usage and Billing",
    url: "/usage",
    icon: BarChart,
  },
  {
    title: "API Key",
    url: "/api-key",
    icon: Key,
  },
  {
    title: "Docs",
    url: "/docs",
    icon: Book,
  },
];

export function AppSidebar() {
  return (
    <Sidebar>
      <SidebarHeader className="flex h-16 items-center justify-center">
        <TextLogo />
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent className="px-4">
            <SidebarMenu>
              {items.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild>
                    <Link href={item.url}>
                      <item.icon />
                      <span>{item.title}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-border/40 border-t px-4 pt-4 pb-6">
        <div className="flex flex-col space-y-4">
          <CustomUserButton showDetails />

          <div className="text-muted-foreground/60 pt-1 text-xs">
            <p>© <CurrentYear /> LazyCloud</p>
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
