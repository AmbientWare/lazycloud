import { act, fireEvent, render, screen } from "@testing-library/react";
import { Toaster, toast } from "sonner";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CopyButton } from "./index";

beforeEach(() => vi.useFakeTimers());

afterEach(() => {
  toast.dismiss();
});

it("reports a denied copy and allows a successful retry", async () => {
  let allowed = false;
  vi.stubGlobal("navigator", {
    clipboard: {
      writeText: async () => {
        if (!allowed) throw new DOMException("Permission denied", "NotAllowedError");
      },
    },
  });
  render(
    <>
      <CopyButton value="token-value" label="token" />
      <Toaster />
    </>,
  );
  const button = screen.getByRole("button", { name: "Copy token" });
  await act(async () => fireEvent.click(button));
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(button).not.toHaveAttribute("title", "Copied");
  expect(screen.getByText(/could not copy/i)).toBeVisible();

  allowed = true;
  await act(async () => fireEvent.click(button));
  expect(button).toHaveAttribute("title", "Copied");

  allowed = false;
  await act(async () => fireEvent.click(button));
  expect(button).not.toHaveAttribute("title", "Copied");
});

it("reports unavailable clipboard access without claiming success", async () => {
  vi.stubGlobal("navigator", {});
  render(
    <>
      <CopyButton value="command" label="command" />
      <Toaster />
    </>,
  );
  const button = screen.getByRole("button", { name: "Copy command" });
  await act(async () => fireEvent.click(button));
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(button).not.toHaveAttribute("title", "Copied");
  expect(screen.getByText(/clipboard access is unavailable/i)).toBeVisible();
});

it("ignores superseded completions and failures after the control closes", async () => {
  let finishFirst: (() => void) | undefined;
  let rejectSecond: ((error: Error) => void) | undefined;
  const first = new Promise<void>((resolve) => {
    finishFirst = resolve;
  });
  const second = new Promise<void>((_, reject) => {
    rejectSecond = reject;
  });
  let write = first;
  vi.stubGlobal("navigator", { clipboard: { writeText: () => write } });
  const view = render(<CopyButton value="result" label="result" />);
  render(<Toaster />);
  const button = screen.getByRole("button", { name: "Copy result" });
  fireEvent.click(button);
  write = second;
  fireEvent.click(button);

  await act(async () => finishFirst?.());
  expect(button).not.toHaveAttribute("title", "Copied");

  view.unmount();
  await act(async () => rejectSecond?.(new Error("Clipboard write cancelled")));
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.queryByText(/could not copy/i)).not.toBeInTheDocument();
});
