import { expect, test, type Page } from "@playwright/test";

/*
 * Journeys against a running platform: the dashboard dev server proxying to a
 * real server, scheduler and agent. Nothing here is mocked.
 *
 *   WEB_E2E_STACK=1        run these journeys
 *   WEB_E2E_SESSION        a browser session cookie value, from
 *                          `server admin create-session --email <admin>`
 *   WEB_E2E_TOKEN          an API token of the same platform administrator
 *   WEB_E2E_WORKSPACE      a workspace the administrator owns
 *   WEB_E2E_APP            an app deployed there with the SDK whose function
 *                          `greet(name: str, times: int = 1)` prints
 *                          "greeting <name>" and returns "hello <name>", and
 *                          whose `report(title: str)` saves report.txt as an
 *                          artifact of its task
 *   WEB_E2E_INVITATION     optional: an invitation link to WEB_E2E_WORKSPACE,
 *                          read from the queued email, and
 *   WEB_E2E_GUEST_SESSION  a session of the account it invites
 *
 * GitHub sign-in needs a GitHub App the local platform does not have, so the
 * session comes from the admin command; the sign-in journey checks the path a
 * browser takes up to GitHub.
 */
const stack = process.env.WEB_E2E_STACK === "1";
const session = process.env.WEB_E2E_SESSION ?? "";
const token = process.env.WEB_E2E_TOKEN ?? "";
const workspace = process.env.WEB_E2E_WORKSPACE ?? "";
const app = process.env.WEB_E2E_APP ?? "";
const invitation = process.env.WEB_E2E_INVITATION ?? "";
const guestSession = process.env.WEB_E2E_GUEST_SESSION ?? "";

test.skip(!stack, "WEB_E2E_STACK=1 and a running platform are required");
// A first call may wait for a cold container and an image pull.
test.setTimeout(90_000);

const startSignIn = `/auth/github/start?return_to=${encodeURIComponent("/callback#code=%2Fdashboard")}`;

/**
 * Open a session in the browser the way a returning visitor does: with the
 * cookie set, starting sign-in skips GitHub and lands on the callback page.
 */
async function signIn(page: Page, baseURL: string, value = session) {
  const { hostname } = new URL(baseURL);
  await page.context().addCookies([
    {
      name: "__Host-lazycloud_session",
      value,
      domain: hostname,
      path: "/",
      secure: true,
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
  await page.goto(startSignIn);
  await page.waitForURL(
    (url) => !url.pathname.startsWith("/auth/") && url.pathname !== "/callback",
  );
}

/** The dashboard addresses apps by ID; the journeys know the app by name. */
async function appId(page: Page, name = app): Promise<string> {
  const response = await page.request.get(`/v1/workspaces/${workspace}/apps?limit=1000`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const { apps } = (await response.json()) as { apps: { id: string; name: string }[] };
  const found = apps.find((item) => item.name === name);
  if (!found) throw new Error(`app ${name} is not deployed to ${workspace}`);
  return found.id;
}

function watchFailures(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(error.message));
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/v1/") && response.status() >= 500) {
      failures.push(`${response.status()} ${path}`);
    }
  });
  return failures;
}

test("a signed-out visitor is sent to sign in, and a session opens the dashboard", async ({
  page,
  baseURL,
}) => {
  await page.goto("/dashboard");
  const signInLink = page.getByRole("link", { name: "Continue with GitHub" });
  await expect(signInLink).toHaveAttribute(
    "href",
    `/auth/github/start?return_to=${encodeURIComponent("/callback#code=%2Fdashboard")}`,
  );
  await signInLink.click();
  // No GitHub App is configured on a local platform, which the server reports.
  await expect(page).toHaveURL(/\/signin\?error=provider_unavailable$/);
  await expect(page.getByRole("alert")).toHaveText(
    "Signing in is unavailable right now. Try again shortly.",
  );

  // A live session skips GitHub entirely.
  await signIn(page, baseURL!);
  await expect(page).toHaveURL(new RegExp(`/w/${workspace}/apps$`));
  await expect(page.getByRole("heading", { name: "Apps", exact: true })).toBeVisible();
});

test("an administrator creates, renames, invites to and deletes a workspace", async ({
  page,
  baseURL,
}) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  const name = `e2e-${Date.now().toString(36)}`;
  await page.goto(`/w/${workspace}/apps`);

  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: "Create workspace" }).click();
  await page.getByRole("dialog").getByRole("textbox").fill(name);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${name}/apps$`));
  await expect(page.getByText("Run your first function")).toBeVisible();

  const renamed = `${name}-x`;
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: `Manage ${name} workspace` }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  await page.getByRole("textbox", { name: "Workspace name" }).fill(renamed);
  await page.getByRole("button", { name: "Rename", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${renamed}/apps$`));

  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: `Manage ${renamed} workspace` }).click();
  await page.getByRole("menuitem", { name: "Members" }).click();
  const members = page.getByRole("dialog", { name: `${renamed} members` });
  await members.getByRole("textbox", { name: "Email address to invite" }).fill("guest@example.com");
  await members.getByRole("button", { name: "Invite" }).click();
  await expect(page.getByText("Invitation sent to guest@example.com")).toBeVisible();
  await members.getByRole("button", { name: "Revoke the invitation to guest@example.com" }).click();
  await expect(members.getByText("guest@example.com")).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: `Manage ${renamed} workspace` }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm.getByRole("button", { name: "Delete permanently" })).toBeDisabled();
  await confirm.getByRole("textbox").fill(renamed);
  await confirm.getByRole("button", { name: "Delete permanently" }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${workspace}/apps$`));
  expect(failures).toEqual([]);
});

test("a deployed app is listed, invoked from the playground and its task opened with logs", async ({
  page,
  baseURL,
}) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  await page.goto(`/w/${workspace}/apps`);

  const id = await appId(page);
  await page.getByRole("link", { name: app, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${workspace}/apps/${id}$`));
  await page.getByRole("link", { name: /greet/ }).first().click();
  await expect(page.getByRole("heading", { name: "greet" })).toBeVisible();

  const who = `e2e ${Date.now().toString(36)}`;
  await page.getByLabel("name").fill(who);
  await page.getByRole("button", { name: "Invoke" }).click();
  // A cold container starts for the first call.
  await expect(page.getByText(`"hello ${who}"`)).toBeVisible({ timeout: 60_000 });

  await page.getByRole("link", { name: "Open task" }).click();
  const drawer = page.getByRole("dialog", { name: "greet" });
  await expect(drawer.getByText("complete", { exact: true })).toBeVisible();
  await drawer.getByRole("tab", { name: "Logs" }).click();
  await expect(drawer.getByRole("list", { name: "Log output" })).toContainText(`greeting ${who}`);
  await drawer.getByRole("tab", { name: "Container" }).click();
  await expect(drawer.getByText("Container ID")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto(`/w/${workspace}/tasks`);
  await expect(page.getByRole("heading", { name: "Tasks", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "greet" }).first()).toBeVisible();
  expect(failures).toEqual([]);
});

test("an access token is created, shown once and deleted", async ({ page, baseURL }) => {
  await signIn(page, baseURL!);
  await page.goto(`/w/${workspace}/apps?settings=tokens`);
  const name = `e2e-${Date.now().toString(36)}`;
  await page.getByRole("button", { name: "Create token" }).first().click();
  await page.getByPlaceholder("ci-deploy").fill(name);
  await page.getByRole("button", { name: "Create token" }).last().click();
  await expect(page.getByText("Token created")).toBeVisible();
  await expect(page.getByText(/^lc_.{3}•+$/)).toBeVisible();
  await page.getByRole("button", { name: "Reveal token value" }).click();
  await expect(page.getByText(/^lc_[A-Za-z0-9_-]{43}$/)).toBeVisible();
  await page.getByRole("button", { name: "Done" }).click();
  // The value is gone from the page once dismissed.
  await expect(page.getByText(/^lc_[A-Za-z0-9_-]{43}$/)).toHaveCount(0);
  await page.getByRole("button", { name: `Delete ${name}` }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("button", { name: `Delete ${name}` })).toHaveCount(0);
});

test("a waiting CLI is approved at /activate", async ({ page, baseURL, request }) => {
  const start = await request.post("/v1/device-codes", { data: { client_name: "cli@e2e" } });
  expect(start.status()).toBe(201);
  const login = (await start.json()) as { device_code: string; user_code: string };

  await signIn(page, baseURL!);
  await page.goto(`/activate?code=${login.user_code}`);
  await expect(page.getByText("cli@e2e")).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("CLI connected")).toBeVisible();

  // The CLI's next poll picks up its token once the interval has passed.
  await page.waitForTimeout(5_500);
  const poll = await request.post("/v1/device-codes/token", {
    data: { device_code: login.device_code },
  });
  const answer = (await poll.json()) as { status: string; token?: string };
  expect(answer.status).toBe("approved");
  const me = await request.get("/v1/me", { headers: { Authorization: `Bearer ${answer.token}` } });
  expect(me.status()).toBe(200);
});

test("a secret is created masked, revealed on request and deleted", async ({ page, baseURL }) => {
  await signIn(page, baseURL!);
  const name = `E2E_${Date.now().toString(36).toUpperCase()}`;
  await page.goto(`/w/${workspace}/storage?view=secrets`);
  await page.getByRole("button", { name: "New secret" }).click();
  await page.getByPlaceholder("SECRET_NAME").fill(name);
  await page.getByLabel("Value", { exact: true }).fill("test-only-value");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("button", { name: `Reveal secret ${name}` })).toBeVisible();
  await expect(page.getByText("test-only-value")).toHaveCount(0);
  await page.getByRole("button", { name: `Reveal secret ${name}` }).click();
  await expect(page.getByText("test-only-value")).toBeVisible();
  await page.getByRole("button", { name: `Delete secret ${name}` }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("button", { name: `Reveal secret ${name}` })).toHaveCount(0);

  // Leave the workspace's API state as it was found.
  const secrets = await page.request.get(`/v1/workspaces/${workspace}/secrets`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(((await secrets.json()) as { secrets: { name: string }[] }).secrets).not.toContainEqual(
    expect.objectContaining({ name }),
  );
});

test("a volume is created, a file uploaded, listed, downloaded and removed", async ({
  page,
  baseURL,
}) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  const name = `e2e-${Date.now().toString(36)}`;
  await page.goto(`/w/${workspace}/storage?view=volumes`);
  await page.getByRole("button", { name: "New volume" }).click();
  await page.getByPlaceholder("volume-name").fill(name);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByRole("button", { name, exact: true }).click();

  await page.locator('input[type="file"]').setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("written from the dashboard\n"),
  });
  await expect(page.getByRole("button", { name: "Download notes.txt" })).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download notes.txt" }).click();
  expect((await download).suggestedFilename()).toBe("notes.txt");

  await page.getByRole("button", { name: "Delete notes.txt" }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("Empty directory")).toBeVisible();
  await page.getByRole("button", { name: `Delete volume ${name}` }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test("a queue takes a message, shows its head and gives it up", async ({ page, baseURL }) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  const name = `e2e-${Date.now().toString(36)}`;
  await page.goto(`/w/${workspace}/storage?view=queues`);
  await page.getByRole("button", { name: "New queue" }).click();
  await page.getByLabel("Queue name").fill(name);
  await page.getByLabel("Message (JSON)").fill('{"job": 1}');
  await page.getByRole("button", { name: "Add message" }).click();
  const inspector = page.getByRole("region", { name: `${name} queue inspector` });
  await expect(inspector).toContainText('"job": 1');
  await inspector.getByRole("button", { name: "Remove next message" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove next message" }).click();
  await inspector.getByRole("button", { name: "Delete queue" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete queue" }).click();
  await expect(page.getByRole("button", { name: new RegExp(name) })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test("a map key is added, edited with a revision check and deleted", async ({ page, baseURL }) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  const name = `e2e-${Date.now().toString(36)}`;
  await page.goto(`/w/${workspace}/storage?view=maps`);
  await page.getByRole("button", { name: "New map" }).click();
  await page.getByLabel("Map name").fill(name);
  await page.getByLabel("Key", { exact: true }).fill("status");
  await page.getByLabel("Value (JSON)").fill('{"ready": false}');
  await page.getByLabel("Expiry").selectOption("never");
  await page.getByRole("button", { name: "Add key" }).click();
  const inspector = page.getByRole("region", { name: `${name} map inspector` });
  await expect(inspector).toContainText('"ready": false');

  await inspector.getByRole("button", { name: "Edit value" }).click();
  const editor = page.getByRole("dialog", { name: "Edit value" });
  await editor.getByLabel("Value (JSON)").fill('{"ready": true}');
  await editor.getByRole("button", { name: "Save value" }).click();
  await expect(inspector).toContainText('"ready": true');

  await inspector.getByRole("button", { name: "Delete map" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete map" }).click();
  await expect(page.getByRole("button", { name: new RegExp(name) })).toHaveCount(0);
  expect(failures).toEqual([]);
});

test("an artifact a task saved is previewed from the task and listed in storage", async ({
  page,
  baseURL,
}) => {
  const failures = watchFailures(page);
  await signIn(page, baseURL!);
  const title = `e2e ${Date.now().toString(36)}`;
  await page.goto(`/w/${workspace}/apps/${await appId(page)}/workloads/function/report`);
  await page.getByLabel("title").fill(title);
  await page.getByRole("button", { name: "Invoke" }).click();
  const outcome = page
    .locator("section")
    .filter({ has: page.getByRole("link", { name: "Open task" }) });
  await expect(outcome.getByText("complete", { exact: true })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: "Open task" }).click();

  const drawer = page.getByRole("dialog", { name: "report" });
  await drawer.getByRole("tab", { name: "Artifacts" }).click();
  await drawer.getByRole("button", { name: "Preview report.txt" }).click();
  await expect(page.getByText(`report ${title}`)).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");

  await page.goto(`/w/${workspace}/storage?view=artifacts`);
  await expect(page.getByRole("button", { name: "Preview report.txt" }).first()).toBeVisible();
  expect(failures).toEqual([]);
});

test("an invited account previews the invitation and joins the workspace", async ({
  page,
  baseURL,
}) => {
  test.skip(!invitation || !guestSession, "WEB_E2E_INVITATION and WEB_E2E_GUEST_SESSION");
  await signIn(page, baseURL!, guestSession);
  await page.goto(new URL(invitation).pathname);
  await expect(page.getByRole("heading", { name: `Join ${workspace}` })).toBeVisible();
  await expect(page.getByText("You will join as the account you are signed in as")).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${workspace}/apps$`));

  // The link is spent.
  await page.goto(new URL(invitation).pathname);
  await expect(
    page.getByRole("heading", { name: "This invitation is no longer open" }),
  ).toBeVisible();
});
