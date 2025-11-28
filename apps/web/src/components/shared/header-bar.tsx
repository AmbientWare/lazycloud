import { type ReactNode } from "react";
import TextLogo from "@/app/_components/textLogo";

type HeaderBarProps = {
  children?: ReactNode;
};

export default function HeaderBar({ children }: HeaderBarProps) {
  return (
    <div className="sticky top-0 z-50 w-full px-4 pt-4">
      <div className="mx-auto max-w-7xl">
        <div className="border-border/60 bg-background/80 flex h-14 items-center justify-between rounded-2xl border px-6 shadow-lg backdrop-blur-md">
          <TextLogo />
          {children}
        </div>
      </div>
    </div>
  );
}
