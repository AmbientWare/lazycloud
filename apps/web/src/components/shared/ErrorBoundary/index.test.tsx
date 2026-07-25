import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PanelErrorBoundary } from ".";

function Bomb({ armed }: { armed: boolean }) {
  if (armed) throw new Error("schema drift broke this panel");
  return <div>panel content</div>;
}

describe("PanelErrorBoundary", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("contains a throwing child to its own fallback while siblings keep rendering", () => {
    render(
      <div>
        <p>sibling panel</p>
        <PanelErrorBoundary title="Logs could not be displayed">
          <Bomb armed />
        </PanelErrorBoundary>
      </div>,
    );

    const fallback = screen.getByRole("alert");
    expect(fallback).toHaveTextContent("Logs could not be displayed");
    expect(fallback).toHaveTextContent("schema drift broke this panel");
    expect(screen.getByText("sibling panel")).toBeInTheDocument();
    expect(screen.queryByText("panel content")).not.toBeInTheDocument();
  });

  it("retry resets the boundary and renders the recovered child", () => {
    const { rerender } = render(
      <PanelErrorBoundary title="Trace could not be displayed">
        <Bomb armed />
      </PanelErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(
      <PanelErrorBoundary title="Trace could not be displayed">
        <Bomb armed={false} />
      </PanelErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("panel content")).toBeInTheDocument();
  });

  it("falls back to a generic message for a non-Error throw", () => {
    function StringBomb(): never {
      throw "not an Error instance";
    }
    render(
      <PanelErrorBoundary title="Output could not be displayed">
        <StringBomb />
      </PanelErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "An unexpected rendering error occurred.",
    );
  });
});
