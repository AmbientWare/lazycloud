"use client";

import * as React from "react";
import { DocsSidebar } from "./_components/docSidebar";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { usePathname } from "next/navigation";
import { UserProvider } from "@/contexts/UserContext";
import TextLogo from "../_components/textLogo";

const titleMap: Record<string, string> = {
  docs: "Documentation",
  init: "Init",
  deploy: "Deploy",
  destroy: "Destroy",
  rollback: "Rollback",
  dashboard: "Dashboard",
  workspaces: "Workspaces",
  deployments: "Deployments",
  usage: "Usage",
};

function DocsHeader() {
  const pathname = usePathname();
  const pathSegments = pathname.split("/").filter(Boolean);

  const breadcrumbs = pathSegments.map((segment, index) => {
    const path = `/${pathSegments.slice(0, index + 1).join("/")}`;
    const isLast = index === pathSegments.length - 1;
    const title = titleMap[segment] ?? segment.charAt(0).toUpperCase() + segment.slice(1);

    return {
      title,
      path,
      isLast,
    };
  });

  return (
    <div className="z-50 w-full">
      <div className="border-border/60 bg-background/80 flex h-14 items-center justify-between rounded-2xl border px-4 shadow-lg backdrop-blur-md">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <SidebarTrigger className="shrink-0" />
          <Breadcrumb className="min-w-0">
            <BreadcrumbList className="flex-wrap">
              {breadcrumbs.map((crumb) => (
                <React.Fragment key={crumb.path}>
                  <BreadcrumbItem className="truncate">
                    {crumb.isLast ? (
                      <BreadcrumbPage className="truncate">
                        {crumb.title}
                      </BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink href={crumb.path} className="truncate">
                        {crumb.title}
                      </BreadcrumbLink>
                    )}
                  </BreadcrumbItem>
                  {!crumb.isLast && <BreadcrumbSeparator className="shrink-0" />}
                </React.Fragment>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
        </div>
        <div className="ml-4 flex shrink-0 items-center gap-6">
          <TextLogo />
        </div>
      </div>
    </div>
  );
}

export default function DocsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <UserProvider>
      <SidebarProvider>
        <DocsSidebar />
        <SidebarInset className="bg-transparent">
          <div className="flex h-full w-full flex-col overflow-hidden p-4">
            <div className="mx-auto w-full max-w-5xl">
              <DocsHeader />
            </div>
            <div className="mx-auto mt-4 flex w-full max-w-5xl flex-1 flex-col overflow-y-auto rounded-xl border border-border/60 bg-card/90 shadow-sm backdrop-blur-md">
              <main className="flex-1 p-6 lg:p-8">{children}</main>
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </UserProvider>
  );
}
