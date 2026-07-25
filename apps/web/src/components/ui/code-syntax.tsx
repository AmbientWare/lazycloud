import { Fragment, type ReactNode } from "react";

/* Marketing-only syntax colouring for the Python and shell snippets shown in
 * CodeWindow. Small on purpose: the snippets are authored in this repository,
 * so the tokenizer covers exactly the constructs they use. */

const PYTHON_KEYWORDS = new Set([
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

const PYTHON_CONSTANTS = new Set(["False", "None", "True", "self", "cls"]);

const PYTHON_BUILTINS = new Set([
  "bool",
  "bytes",
  "dict",
  "float",
  "int",
  "len",
  "list",
  "print",
  "range",
  "set",
  "str",
  "sum",
  "tuple",
]);

const PYTHON_TOKEN =
  /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|[a-zA-Z]?"(?:[^"\\\n]|\\.)*"|[a-zA-Z]?'(?:[^'\\\n]|\\.)*')|(@[A-Za-z_][\w.]*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)/g;

const SHELL_TOKEN =
  /(#[^\n]*)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(^\s*\$)|(\s-{1,2}[A-Za-z][\w-]*)/gm;

type Token = { className: string | null; value: string };

function pythonWordClass(word: string, after: string): string | null {
  if (PYTHON_KEYWORDS.has(word)) return "tok-keyword";
  if (PYTHON_CONSTANTS.has(word)) return "tok-constant";
  if (after.startsWith("=") && !after.startsWith("==")) return "tok-param";
  if (after.startsWith("(")) {
    return PYTHON_BUILTINS.has(word) ? "tok-builtin" : "tok-call";
  }
  if (PYTHON_BUILTINS.has(word)) return "tok-builtin";
  if (/^[A-Z]/.test(word)) return "tok-type";
  return null;
}

function tokenize(code: string, pattern: RegExp, wordClass: string[]): Token[] {
  const tokens: Token[] = [];
  let cursor = 0;

  pattern.lastIndex = 0;
  for (
    let match = pattern.exec(code);
    match !== null;
    match = pattern.exec(code)
  ) {
    if (match.index > cursor) {
      tokens.push({ className: null, value: code.slice(cursor, match.index) });
    }

    const groupIndex = match.findIndex(
      (group, index) => index > 0 && group !== undefined,
    );
    const value = match[0];
    const isWordGroup = groupIndex === wordClass.length + 1;
    const className = isWordGroup
      ? pythonWordClass(value, code.slice(match.index + value.length))
      : (wordClass[groupIndex - 1] ?? null);

    tokens.push({ className, value });
    cursor = match.index + value.length;
  }

  if (cursor < code.length) {
    tokens.push({ className: null, value: code.slice(cursor) });
  }
  return tokens;
}

export function highlight(code: string): ReactNode {
  const isShell = /^\s*\$/.test(code);
  const tokens = isShell
    ? tokenize(code, SHELL_TOKEN, [
        "tok-comment",
        "tok-string",
        "tok-prompt",
        "tok-param",
      ])
    : tokenize(code, PYTHON_TOKEN, [
        "tok-comment",
        "tok-string",
        "tok-decorator",
        "tok-number",
      ]);

  return tokens.map((token, index) =>
    token.className ? (
      <span className={token.className} key={`${index}-${token.value}`}>
        {token.value}
      </span>
    ) : (
      <Fragment key={`${index}-${token.value}`}>{token.value}</Fragment>
    ),
  );
}
