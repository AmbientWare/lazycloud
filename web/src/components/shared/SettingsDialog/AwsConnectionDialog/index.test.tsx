import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "@/lib/api/client";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

import { AwsConnectionDialog } from ".";

const STACK_URL =
  "https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#/stacks/quickcreate";

describe("AWS connection dialog", () => {
  it("offers exactly the actions the API allows", () => {
    const { rerender } = renderDialog(
      connection("reconnect_pending", {
        available_actions: ["authorize", "validate", "cancel_reconnect", "remove"],
        customer_action: { label: "Create the connection stack", url: STACK_URL },
      }),
    );

    const link = screen.getByRole("link", { name: "Open AWS Console" });
    expect(link).toHaveAttribute("href", STACK_URL);
    expect(link).toHaveAttribute("target", "_blank");
    expect(buttonNames()).toEqual(["Check authorization", "Cancel reconnect", "Remove connection"]);

    rerender(dialog(connection("action_required", { available_actions: ["retry", "forget"] })));
    expect(screen.queryByRole("link")).toBeNull();
    expect(buttonNames()).toEqual(["Retry", "Forget connection"]);
  });

  it("shows where the customer's stack stands during setup", () => {
    const failedStack = "https://us-east-2.console.aws.amazon.com/cloudformation/home#/stacks";
    const setup = (
      setup_step: Schemas["AwsSetupStep"],
      fields: Partial<Schemas["AwsConnection"]> = {},
    ) =>
      connection("awaiting_authorization", {
        active_authorization: undefined,
        pending_authorization: {
          generation: 1,
          authorization_mode: "managed_stack",
          phase: "degraded",
          setup_step,
          error_message: "Validation failed with 1 error(s).",
          created_at: "2026-07-20T00:00:00Z",
          updated_at: "2026-07-20T00:00:01Z",
        },
        available_actions: ["authorize", "validate", "remove"],
        console_url: STACK_URL,
        ...fields,
      });
    const links = () =>
      screen.queryAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")]);

    const { rerender } = renderDialog(setup("waiting_for_stack"));
    expect(links()).toEqual([["Open AWS Console", STACK_URL]]);
    expect(buttonNames()).toEqual(["Check now", "Cancel setup"]);

    rerender(dialog(setup("stack_creating")));
    expect(screen.getByText("AWS is creating the stack")).toBeInTheDocument();
    expect(links()).toEqual([]);

    rerender(
      dialog(
        setup("stack_failed", {
          customer_action: { label: "Delete the failed stack in AWS", url: failedStack },
        }),
      ),
    );
    expect(screen.getByText("Validation failed with 1 error(s).")).toBeInTheDocument();
    expect(links()).toEqual([
      ["Delete the stack", failedStack],
      ["Create again", STACK_URL],
    ]);
  });

  it("forgets after confirmation and links what stays in AWS", async () => {
    const leftovers: Schemas["AwsLeftover"][] = [
      {
        kind: "stack",
        id: "lazycloud-connection",
        region: "us-east-2",
        console_url: "https://console.aws.amazon.com/cloudformation/stack",
      },
      {
        kind: "instance",
        id: "i-0123456789abcdef0",
        region: "us-west-2",
        console_url: "https://console.aws.amazon.com/ec2/instance",
      },
    ];
    const sent: string[] = [];
    vi.stubGlobal("fetch", async (request: Request) => {
      sent.push(`${request.method} ${new URL(request.url).pathname}`);
      return Response.json({ left_in_aws: leftovers });
    });
    const { queryClient } = renderDialog(
      connection("action_required", { available_actions: ["retry", "forget"] }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Forget connection" }));
    const confirm = screen.getByRole("alertdialog");
    expect(sent).toEqual([]);
    fireEvent.click(within(confirm).getByRole("button", { name: "Forget connection" }));

    await screen.findByText("lazycloud-connection");
    expect(sent).toEqual(["POST /v1/aws-connection/forget"]);
    expect(queryClient.getQueryData(accountQueryKeys.compute.awsConnection())).toBeNull();
    expect(screen.getByText("i-0123456789abcdef0")).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: "Open" }).map((link) => link.getAttribute("href")),
    ).toEqual(leftovers.map((leftover) => leftover.console_url));
  });
});

function dialog(value: Schemas["AwsConnection"]) {
  return <AwsConnectionDialog connection={value} open onOpenChange={() => undefined} />;
}

function renderDialog(value: Schemas["AwsConnection"]) {
  const queryClient = testQueryClient();
  const rendered = render(dialog(value), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  });
  return { ...rendered, queryClient };
}

/** The dialog's buttons, without its close control. */
function buttonNames() {
  return within(screen.getByRole("dialog"))
    .getAllByRole("button")
    .map((button) => button.textContent)
    .filter((name) => name !== "Close");
}

function connection(
  phase: Schemas["AwsConnection"]["phase"],
  fields: Partial<Schemas["AwsConnection"]>,
): Schemas["AwsConnection"] {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    account_id: "123456789012",
    phase,
    active_authorization: {
      generation: 1,
      authorization_mode: "managed_stack",
      phase: "ready",
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:01Z",
    },
    revision: 1,
    hosts_workloads: false,
    can_manage_existing_capacity: false,
    available_actions: [],
    detail: "AWS connection state",
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:01Z",
    ...fields,
  };
}
