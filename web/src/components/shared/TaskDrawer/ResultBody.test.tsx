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
  expect(document).toContain("<td>1</td>");
  expect(document).not.toContain("<script");
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

it("removes what would redirect the frame, load from the network or run code", () => {
  render(
    <ResultBody
      error={null}
      result={{
        encoding: "cloudpickle",
        data: pickle,
        display: {
          text: "x",
          rich: {
            kind: "html",
            html:
              '<meta http-equiv="refresh" content="0;url=https://evil.example/">' +
              '<base href="https://evil.example/"><link rel="stylesheet" href="https://evil.example/a.css">' +
              '<a href="https://evil.example/login" target="_top">sign in</a>' +
              '<img src="https://evil.example/p.png" onerror="alert(1)"><img src="data:image/png;base64,AA==">' +
              '<iframe src="https://evil.example"></iframe><form action="https://evil.example"><input></form>' +
              "<p>kept</p>",
          },
        },
      }}
    />,
  );

  const document = screen.getByTitle("Rendered result").getAttribute("srcdoc") ?? "";
  const body = document.slice(document.indexOf("<body>"));
  expect(body).not.toContain("evil.example");
  expect(body).not.toMatch(/<meta|<base|<link|<iframe|<form|onerror|target=/);
  expect(body).toContain("<a>sign in</a>");
  expect(body).toContain('<img src="data:image/png;base64,AA==">');
  expect(body).toContain("<p>kept</p>");
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
