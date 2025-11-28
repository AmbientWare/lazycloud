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
import { Separator } from "@/components/ui/separator";
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { usePathname } from "next/navigation";
import { UserProvider } from "@/contexts/UserContext";
import TextLogo from "../_components/textLogo";

function DocsHeader() {
  const pathname = usePathname();
  const pathSegments = pathname.split("/").filter(Boolean);

  const breadcrumbs = pathSegments.map((segment, index) => {
    const path = `/${pathSegments.slice(0, index + 1).join("/")}`;
    const isLast = index === pathSegments.length - 1;
    const title = segment.charAt(0).toUpperCase() + segment.slice(1);

    return {
      title,
      path,
      isLast,
    };
  });

  return (
    <header className="flex h-16 shrink-0 items-center justify-between border-b">
      <div className="flex min-w-0 flex-1 items-center gap-2 px-3">
        <SidebarTrigger className="shrink-0" />
        <Separator orientation="vertical" className="mr-2 h-4 shrink-0" />
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
      <div className="ml-4 flex shrink-0 items-center gap-6 px-6">
        <TextLogo />
      </div>
    </header>
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
        <SidebarInset>
          <DocsHeader />
          <div className="flex flex-1 flex-col gap-4 p-4">{children}</div>
        </SidebarInset>
      </SidebarProvider>
    </UserProvider>
  );
}
