"use client";

import * as React from "react";
import { BookOpen, ChevronRight } from "lucide-react";
import { usePathname } from "next/navigation";

import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import Link from "next/link";
import { cn } from "@/lib/utils";

const data = {
  navMain: [
    {
      title: "Getting Started",
      url: "/docs",
      items: [],
    },
    {
      title: "Compose Labels",
      url: "/docs/labels",
      items: [
        {
          title: "Service Labels",
          url: "/docs/labels/service",
        },
        {
          title: "Scaling Labels",
          url: "/docs/labels/scaling",
        },
        {
          title: "Volume Labels",
          url: "/docs/labels/volume",
        },
      ],
    },
    {
      title: "CLI Commands",
      url: "/docs/init",
      items: [
        {
          title: "Init",
          url: "/docs/init",
        },
        {
          title: "Deploy",
          url: "/docs/deploy",
        },
        {
          title: "Destroy",
          url: "/docs/destroy",
        },
        {
          title: "Rollback",
          url: "/docs/rollback",
        },
        {
          title: "Dashboard",
          url: "/docs/dashboard",
        },
        {
          title: "Workspaces",
          url: "/docs/workspaces",
        },
        {
          title: "Deployments",
          url: "/docs/deployments",
        },
        {
          title: "Usage",
          url: "/docs/usage",
        },
      ],
    },
    {
      title: "Examples",
      url: "/docs/examples",
      items: [
        {
          title: "FastAPI LLM",
          url: "/docs/examples/fastapi-llm",
        },
        {
          title: "Go API",
          url: "/docs/examples/go-api",
        },
        {
          title: "Next.js",
          url: "/docs/examples/nextjs",
        },
      ],
    },
  ],
};

export function DocsSidebar() {
  const pathname = usePathname();

  const isActive = (url: string) => pathname === url;
  const isParentActive = (item: (typeof data.navMain)[0]) => {
    if (pathname === item.url) return true;
    return item.items?.some((sub) => pathname === sub.url) ?? false;
  };

  return (
    <Sidebar>
      <SidebarHeader>
        <div className="flex items-center gap-3 px-2 py-2">
          <div className="bg-lazycloud/10 flex aspect-square size-8 items-center justify-center rounded-lg">
            <BookOpen className="text-lazycloud size-4" />
          </div>
          <div className="flex flex-col gap-0.5 leading-none">
            <span className="font-semibold">Documentation</span>
            <span className="text-muted-foreground text-xs">LazyCloud</span>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu>
            {data.navMain.map((item) =>
              item.items?.length ? (
                <Collapsible
                  key={item.title}
                  asChild
                  defaultOpen={isParentActive(item)}
                  className="group/collapsible"
                >
                  <SidebarMenuItem>
                    <CollapsibleTrigger asChild>
                      <SidebarMenuButton
                        className={cn(
                          "font-medium",
                          isParentActive(item) && "text-lazycloud"
                        )}
                      >
                        {item.title}
                        <ChevronRight className="ml-auto transition-transform duration-200 group-data-[state=open]/collapsible:rotate-90" />
                      </SidebarMenuButton>
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <SidebarMenuSub>
                        {item.items.map((subItem) => (
                          <SidebarMenuSubItem key={subItem.title}>
                            <SidebarMenuButton
                              asChild
                              isActive={isActive(subItem.url)}
                            >
                              <Link
                                href={subItem.url}
                                className={cn(
                                  isActive(subItem.url) &&
                                    "text-lazycloud font-medium"
                                )}
                              >
                                <span>{subItem.title}</span>
                              </Link>
                            </SidebarMenuButton>
                          </SidebarMenuSubItem>
                        ))}
                      </SidebarMenuSub>
                    </CollapsibleContent>
                  </SidebarMenuItem>
                </Collapsible>
              ) : (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton asChild isActive={isActive(item.url)}>
                    <Link
                      href={item.url}
                      className={cn(
                        "font-medium",
                        isParentActive(item) && "text-lazycloud"
                      )}
                    >
                      {item.title}
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )
            )}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}
