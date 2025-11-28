"use client";

import { useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  AlertCircle,
  Shield,
  Ban,
  AlertTriangle,
} from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const sections = [
  {
    id: "section-1",
    title: "1. Introduction",
    content: (
      <div className="space-y-4">
        <p>
          Your use of LazyCloud is subject to the LazyCloud Terms of Service and
          this Acceptable Use Policy (AUP). LazyCloud reserves the right to
          suspend or terminate your account, with or without notice, and in its
          sole discretion, if your use of the LazyCloud Services violates this
          AUP.
        </p>

        <p>
          While we cannot provide an exhaustive list of permitted and prohibited
          activities, we encourage creativity while ensuring that your use of
          LazyCloud does not negatively impact LazyCloud, other customers, or
          third parties. The following examples of prohibited uses are provided
          for guidance but are not intended to be comprehensive.
        </p>
      </div>
    ),
  },
  {
    id: "section-2",
    title: "2. Prohibited Uses",
    content: (
      <div className="space-y-4">
        <h3 className="text-lg font-semibold">Examples of Prohibited Uses:</h3>
        <ul className="list-disc space-y-2 pl-6">
          <li>
            <span className="font-medium">Spamming:</span> Sending, proxying, or
            serving spam content through LazyCloud resources
          </li>
          <li>
            <span className="font-medium">Resource Abuse:</span> Bypassing usage
            limits or attempting to circumvent the pre-billing model
          </li>
          <li>
            <span className="font-medium">Cryptomining:</span> Using LazyCloud
            resources for cryptocurrency mining operations
          </li>
          <li>
            <span className="font-medium">Copyright Violations:</span> Hosting
            or distributing content that infringes on intellectual property
            rights, including bots, torrents, or unauthorized mirrors
          </li>
          <li>
            <span className="font-medium">Abusive Services:</span> Operating
            URL/link shorteners or similar services that generate abuse
            complaints
          </li>
          <li>
            <span className="font-medium">Unauthorized Security Testing:</span>{" "}
            Conducting network or application scanning, phishing, or penetration
            testing without explicit consent
          </li>
          <li>
            <span className="font-medium">Harmful Content:</span> Serving
            violent, harassing, or harmful content (including doxxing, revenge
            pornography, or similar materials)
          </li>
          <li>
            <span className="font-medium">Illegal Activities:</span> Engaging in
            any activities that violate applicable laws or regulations
          </li>
          <li>
            <span className="font-medium">Resource Exhaustion:</span> Creating
            instances or workloads that consume excessive resources or degrade
            service for other customers
          </li>
          <li>
            <span className="font-medium">Data Retention Violations:</span>{" "}
            Attempting to circumvent the 24-hour instance lifecycle to store
            data long-term
          </li>
        </ul>

        <p>
          This list is not exhaustive, and LazyCloud reserves the right to take
          appropriate action against any use that, in LazyCloud&apos;s sole
          discretion, violates the spirit of this AUP, even if not explicitly
          listed above.
        </p>
      </div>
    ),
  },
  {
    id: "section-3",
    title: "3. Enforcement",
    content: (
      <div className="space-y-4">
        <p>
          LazyCloud may, in its sole discretion, take any action it deems
          necessary to enforce this AUP, including but not limited to:
        </p>
        <ul className="list-disc space-y-2 pl-6">
          <li>Issuing warnings to users who violate this AUP</li>
          <li>
            Suspending or terminating accounts that repeatedly violate this AUP
          </li>
          <li>Removing or blocking access to content that violates this AUP</li>
          <li>Reporting violations to law enforcement authorities</li>
        </ul>
        <p>
          LazyCloud is not obligated to provide notice before taking action to
          enforce this AUP, except as required by applicable law.
        </p>
      </div>
    ),
  },
  {
    id: "section-4",
    title: "4. Reporting Violations",
    content: (
      <div className="space-y-4">
        <p>
          If you believe that another user is violating this AUP, please report
          the violation to{" "}
          <a
            href="/support"
            className="text-blue-600 hover:underline dark:text-blue-400"
          >
            LazyCloud Support
          </a>
          . Please include as much detail as possible, including:
        </p>
        <ul className="list-disc space-y-2 pl-6">
          <li>The nature of the violation</li>
          <li>
            The identity of the user or account that is violating this AUP
          </li>
          <li>Any relevant evidence or documentation</li>
          <li>Your contact information</li>
        </ul>
        <p>
          LazyCloud will investigate all reports of violations and take
          appropriate action in accordance with this AUP.
        </p>
      </div>
    ),
  },
  {
    id: "section-5",
    title: "5. Questions and Clarifications",
    content: (
      <div className="space-y-4">
        <p>
          If you have questions about whether a particular use is permitted
          under this AUP, please{" "}
          <a
            href="/support"
            className="text-blue-600 hover:underline dark:text-blue-400"
          >
            contact support
          </a>
          . We are happy to provide clarification on specific use cases.
        </p>
        <p>
          LazyCloud may update this AUP from time to time. The most current
          version will be posted on our website. Your continued use of the
          LazyCloud Services after any changes to this AUP constitutes your
          acceptance of the updated AUP.
        </p>
      </div>
    ),
  },
];

export default function AcceptableUsePage() {
  const [expandAll, setExpandAll] = useState(false);
  const effectiveDate = new Date().toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });

  const toggleAll = () => {
    setExpandAll(!expandAll);
  };

  return (
    <main className="flex min-h-screen flex-1 overflow-hidden px-4 pt-20 pb-8 sm:px-6 sm:pt-24 lg:px-8">
      <div className="bg-card border-border/50 mx-auto flex w-full max-w-5xl flex-1 flex-col overflow-hidden rounded-xl border p-6 shadow-sm sm:p-8 lg:p-10">
        <div className="mb-12 text-center">
          <h1 className="mb-3 text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl">
            Acceptable Use Policy
          </h1>
          <p className="text-muted-foreground text-sm">
            Effective date: {effectiveDate}
          </p>
        </div>

        <Card className="border-border/50 mb-8 shadow-sm">
          <CardContent className="p-6">
            <div className="flex flex-col items-start gap-6 md:flex-row md:items-center">
              <div className="bg-muted flex-shrink-0 rounded-full p-3">
                <AlertCircle className="h-6 w-6 text-amber-500" />
              </div>
              <div className="flex-1 space-y-2">
                <h2 className="text-lg font-semibold">Important Notice</h2>
                <p className="text-muted-foreground text-sm">
                  By using LazyCloud, you agree to this Acceptable Use Policy.
                  Please read it carefully. Violations may result in immediate
                  suspension or termination of your account.
                </p>
              </div>
              <div className="mt-4 md:mt-0 md:ml-auto">
                <Button
                  onClick={toggleAll}
                  variant="outline"
                  className="flex items-center gap-2"
                >
                  {expandAll ? (
                    <>
                      <ChevronUp className="h-4 w-4" />
                      <span>Collapse All</span>
                    </>
                  ) : (
                    <>
                      <ChevronDown className="h-4 w-4" />
                      <span>Expand All</span>
                    </>
                  )}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="prose prose-slate dark:prose-invert mb-8 max-w-none">
          <p className="text-foreground pb-4 text-lg">
            Thank you for using LazyCloud! We&apos;re happy you&apos;re here.
            Please read this Acceptable Use Policy carefully before accessing or
            using LazyCloud.
          </p>
          <p className="text-muted-foreground">
            This Acceptable Use Policy (&quot;AUP&quot;) describes the rules and
            guidelines that apply to your use of our website located at
            lazycloud.dev and its subdomains (collectively, the
            &quot;Website&quot;) and the products, services, content and other
            resources available on or enabled via our Website (collectively, the
            &quot;LazyCloud Services&quot;).
          </p>
        </div>

        <div className="mb-12 grid grid-cols-1 gap-6 md:grid-cols-3">
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-emerald-100 p-2 dark:bg-emerald-900/30">
                <Shield className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
              </div>
              <h3 className="text-lg font-semibold">Fair Usage</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              Use our services creatively but responsibly. Don&apos;t negatively
              impact LazyCloud, other customers, or third parties.
            </p>
          </div>
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-red-100 p-2 dark:bg-red-900/30">
                <Ban className="h-5 w-5 text-red-600 dark:text-red-400" />
              </div>
              <h3 className="text-lg font-semibold">Prohibited Activities</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              Spamming, cryptomining, copyright violations, and other harmful
              activities are strictly prohibited.
            </p>
          </div>
          <div className="bg-muted/30 border-border/50 rounded-lg border p-6">
            <div className="mb-4 flex items-center gap-3">
              <div className="rounded-full bg-amber-100 p-2 dark:bg-amber-900/30">
                <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400" />
              </div>
              <h3 className="text-lg font-semibold">Enforcement</h3>
            </div>
            <p className="text-muted-foreground text-sm">
              Violations may result in immediate suspension or termination of
              your account without prior notice.
            </p>
          </div>
        </div>

        <Accordion
          type="multiple"
          defaultValue={expandAll ? sections.map((s) => s.id) : []}
          value={expandAll ? sections.map((s) => s.id) : undefined}
          className="mb-12 space-y-2"
        >
          {sections.map((section) => (
            <AccordionItem
              key={section.id}
              value={section.id}
              className="border-border/50 bg-muted/20 rounded-lg px-4"
            >
              <AccordionTrigger className="py-4 text-xl font-semibold hover:no-underline">
                {section.title}
              </AccordionTrigger>
              <AccordionContent className="text-muted-foreground pb-6">
                {section.content}
              </AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>

        <div className="bg-muted/30 border-border/50 mb-12 rounded-xl border p-6">
          <h2 className="mb-4 text-xl font-bold">Contact Us</h2>
          <p className="text-muted-foreground mb-4">
            If you have any questions about this Acceptable Use Policy, please{" "}
            <a
              href="/support"
              className="text-lazycloud font-medium hover:underline"
            >
              contact support
            </a>
            . To report a violation, please contact{" "}
            <a
              href="/support"
              className="text-lazycloud font-medium hover:underline"
            >
              LazyCloud Support
            </a>
            .
          </p>
        </div>

        <div className="text-muted-foreground text-center text-sm">
          <p>© {new Date().getFullYear()} LazyCloud. All rights reserved.</p>
        </div>
      </div>
    </main>
  );
}
