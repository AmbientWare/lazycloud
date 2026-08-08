import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import { changePasswordMutationOptions } from "@/lib/queries/auth";

export const PASSWORD_MIN_LENGTH = 8;

export type PasswordChangeController = {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
  setCurrentPassword: (value: string) => void;
  setNewPassword: (value: string) => void;
  setConfirmPassword: (value: string) => void;
  /** What is still missing, but only once there is something to be missing from. */
  guidance: string;
  canSubmit: boolean;
  isSaving: boolean;
  error: string;
  submit: () => void;
};

export function usePasswordChangeController({
  userId,
  onChanged,
}: {
  userId: string;
  /** Called once the change lands, so the caller can close what is showing the form. */
  onChanged?: () => void;
}): PasswordChangeController {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const change = useMutation({
    ...changePasswordMutationOptions(),
    onSuccess: () => {
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      onChanged?.();
    },
  });

  const blocker = passwordChangeBlocker({ currentPassword, newPassword, confirmPassword });
  const canSubmit = blocker === "" && !change.isPending;
  // An untouched form has nothing wrong with it. Showing the first unmet rule at
  // rest reads as a complaint about something the person has not done yet.
  const touched = Boolean(currentPassword || newPassword || confirmPassword);

  return {
    currentPassword,
    newPassword,
    confirmPassword,
    setCurrentPassword: (value) => {
      change.reset();
      setCurrentPassword(value);
    },
    setNewPassword: (value) => {
      change.reset();
      setNewPassword(value);
    },
    setConfirmPassword: (value) => {
      change.reset();
      setConfirmPassword(value);
    },
    guidance: touched ? blocker : "",
    canSubmit,
    isSaving: change.isPending,
    error: change.error ? passwordChangeError(change.error) : "",
    submit: () => {
      if (!canSubmit) return;
      change.mutate({ userId, currentPassword, newPassword });
    },
  };
}

/** Stated as one reason at a time, in the order someone fills the form in. */
export function passwordChangeBlocker({
  currentPassword,
  newPassword,
  confirmPassword,
}: {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
}): string {
  if (!currentPassword) return "Enter your current password.";
  if (newPassword.length < PASSWORD_MIN_LENGTH) {
    return `New password must be at least ${PASSWORD_MIN_LENGTH} characters.`;
  }
  if (newPassword === currentPassword) return "New password must differ from the current one.";
  if (newPassword !== confirmPassword) return "The two new passwords do not match.";
  return "";
}

export function passwordChangeError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "That is not your current password.";
  }
  if (error instanceof ApiError && error.status === 400) {
    return "The new password was rejected. Choose a longer one.";
  }
  if (error instanceof ApiError) {
    return `The password could not be changed (${error.status} ${error.statusText}).`;
  }
  return "The password could not be changed.";
}
