import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { ResultBody } from "./ResultBody";

const pickle = btoa("pickled bytes");

it("shows a Python object's rendered HTML in a frame with no scripts or network, and its text", () => {
  render(
    <ResultBody
      error={null}
      result={{
        encoding: "cloudpickle",
        data: pickle,
        display: {
          text: "Table(rows=1)",
          rich: {
            kind: "html",
            html: "<table><tr><td>1</td></tr></table><script>alert(1)</script>",
          },
        },
      }}
    />,
  );

  const frame = screen.getByTitle("Rendered result");
  expect(frame).toHaveAttribute("sandbox", "");
  expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
  const document = frame.getAttribute("srcdoc") ?? "";
  expect(document).toContain("<table><tr><td>1</td></tr></table>");
  expect(document).toContain(
    `content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"`,
  );
  expect(screen.getByRole("button", { name: "Download HTML" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Download Python object" })).toHaveAttribute(
    "title",
    "Download Python object (13 B, pickle)",
  );

  fireEvent.click(screen.getByRole("button", { name: "Text" }));
  expect(screen.queryByTitle("Rendered result")).not.toBeInTheDocument();
  expect(screen.getByText("Table(rows=1)")).toBeVisible();
});

it("shows a Python object's image as a PNG", () => {
  render(
    <ResultBody
      error={null}
      result={{
        encoding: "cloudpickle",
        data: pickle,
        display: {
          text: "<Figure>",
          rich: { kind: "image", media_type: "image/png", value_base64: "iVBORw0KGgo=" },
        },
      }}
    />,
  );

  expect(screen.getByRole("img", { name: "Result image" })).toHaveAttribute(
    "src",
    "data:image/png;base64,iVBORw0KGgo=",
  );
  expect(screen.getByRole("button", { name: "Download image" })).toBeVisible();
});

it("shows a Python object's text when it renders no view, and offers the pickle without one", () => {
  const { rerender } = render(
    <ResultBody
      error={null}
      result={{ encoding: "cloudpickle", data: pickle, display: { text: "{'total': 3}" } }}
    />,
  );
  expect(screen.getByText("{'total': 3}")).toBeVisible();
  expect(screen.queryByRole("group", { name: "Result view" })).not.toBeInTheDocument();

  rerender(<ResultBody error={null} result={{ encoding: "cloudpickle", data: pickle }} />);
  expect(screen.getByText("Python object, 13 B")).toBeVisible();
  expect(screen.getByText("Download it and load it with the Python SDK.")).toBeVisible();
});
