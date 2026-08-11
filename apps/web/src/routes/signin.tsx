import { createFileRoute } from "@tanstack/react-router";

import { SignInScreen } from "@/components/shared/AuthGate/SignInScreen";

/**
 * Where a failed sign-in lands.
 *
 * The API answers a broken GitHub round trip with a redirect rather than JSON,
 * because the caller is a browser navigating and a bare error document is a dead
 * end. This is the page that makes that worth doing: it says what went wrong and
 * offers the only way forward.
 */
export const Route = createFileRoute("/signin")({
  validateSearch: (search: Record<string, unknown>): { error: string } => ({
    error: typeof search.error === "string" ? search.error : "",
  }),
  component: SignInPage,
});

// Closed set, matching what the API is allowed to send. An unrecognized code gets
// the generic message rather than being echoed into the page.
const SIGN_IN_ERRORS: Record<string, string> = {
  access_denied: "GitHub did not authorize the sign-in.",
  invalid_state: "That sign-in link expired or was already used. Try again.",
  invalid_return_to: "That sign-in link was malformed. Try again.",
  account_disabled: "This account is disabled. Ask an administrator to re-enable it.",
  provider_unavailable: "Signing in with GitHub is unavailable right now.",
};

function SignInPage() {
  const { error } = Route.useSearch();
  return <SignInScreen error={error ? (SIGN_IN_ERRORS[error] ?? "Sign-in failed.") : undefined} />;
}
