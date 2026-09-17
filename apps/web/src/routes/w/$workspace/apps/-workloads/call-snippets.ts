import type { DeploymentManifest } from "@/lib/api/schemas";
import { exampleBody, pythonLiteral } from "./playground-form";

type SourceImport = { importLine: string; reference: string };

export function sourceImport(handler: string): SourceImport | null {
  const [module, reference] = handler.split(":");
  if (!module || !reference) return null;
  return { importLine: `from ${module} import ${reference.split(".")[0]}`, reference };
}

export function pythonCall(result: string, method: string, manifest: DeploymentManifest): string {
  const body = exampleBody(manifest);
  const parameters = manifest.client_contract?.operation.parameters;
  const args = parameters
    ? parameters
        .filter(
          (parameter) => !["var_positional", "var_keyword"].includes(parameter.parameter_kind),
        )
        .filter(
          (parameter) => parameter.required || !(parameter.python_type || parameter.python_default),
        )
        .map(
          (parameter) =>
            `${parameter.parameter_kind === "positional_only" ? "" : `${parameter.name}=`}${parameter.python_type ? parameter.name : pythonLiteral(body[parameter.name], 4)}`,
        )
    : Object.keys(body).length
      ? [`**${pythonLiteral(body, 4)}`]
      : [];
  if (!args.length) return `${result} = ${method}()`;
  return `${result} = ${method}(\n${args.map((arg) => `    ${arg},`).join("\n")}\n)`;
}

export function sourceSnippet(manifest: DeploymentManifest, source: SourceImport): string {
  const { importLine, reference } = source;
  const pythonInputs =
    manifest.client_contract?.operation.parameters
      .filter((parameter) => parameter.required && parameter.python_type)
      .map((parameter) => `${parameter.name}: ${parameter.python_type}`) ?? [];
  if (manifest.kind === "asgi")
    return [
      importLine,
      "",
      `response = ${reference}.request(`,
      '    method="GET",',
      '    path="/",',
      '    target="deployed",',
      ")",
      "print(response.status_code, response.text)",
    ].join("\n");
  if (manifest.kind === "endpoint")
    return [
      importLine,
      "",
      pythonCall("response", `${reference}.target("deployed").request`, manifest),
      "print(response.status_code, response.json())",
    ].join("\n");
  return [
    importLine,
    "",
    ...(pythonInputs.length ? [`# Supply Python objects for ${pythonInputs.join(", ")}.`, ""] : []),
    pythonCall("result", `${reference}.remote`, manifest),
    "print(result)",
    "",
    "async def call_async():",
    ...pythonCall("result", `await ${reference}.async_remote`, manifest)
      .split("\n")
      .map((line) => `    ${line}`),
    "    return result",
    "",
    pythonCall("call", `${reference}.spawn`, manifest),
    "print(call.get())",
  ].join("\n");
}
