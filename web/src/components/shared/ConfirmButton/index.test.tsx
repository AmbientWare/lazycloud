import { act, fireEvent, render, screen } from "@testing-library/react";
import { Trash2 } from "lucide-react";
import { afterEach, expect, it, vi } from "vitest";

import { ConfirmButton } from "./index";

afterEach(() => vi.useRealTimers());

function setup() {
  const onConfirm = vi.fn();
  render(<ConfirmButton label="Delete feedback" icon={Trash2} onConfirm={onConfirm} />);
  const trigger = () => screen.getByTitle(/^(Delete feedback|Cancel)$/);
  const confirm = () => screen.getByTitle("Confirm: Delete feedback");
  return { onConfirm, trigger, confirm };
}

it("acts only on the second, deliberate press", () => {
  const { onConfirm, trigger, confirm } = setup();
  expect(confirm()).toBeDisabled();

  fireEvent.click(trigger());
  expect(onConfirm).not.toHaveBeenCalled();
  expect(trigger()).toHaveAccessibleName("Cancel");

  // A double click lands on cancel, which sits where the icon was.
  fireEvent.click(trigger());
  expect(trigger()).toHaveAccessibleName("Delete feedback");
  expect(onConfirm).not.toHaveBeenCalled();

  fireEvent.click(trigger());
  fireEvent.keyDown(trigger(), { key: "Escape" });
  expect(confirm()).toBeDisabled();

  fireEvent.click(trigger());
  fireEvent.click(confirm());
  expect(onConfirm).toHaveBeenCalledOnce();
});

it("folds back when left alone", () => {
  vi.useFakeTimers();
  const { onConfirm, trigger, confirm } = setup();
  fireEvent.click(trigger());
  act(() => vi.advanceTimersByTime(5000));
  expect(confirm()).toBeDisabled();
  expect(onConfirm).not.toHaveBeenCalled();
});
