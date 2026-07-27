import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import fs from "fs";
import path from "path";
import { m as matter } from "../_chunks/_libs/gray-matter.mjs";
import { s as source } from "./source-Zpe9Usb2.mjs";
import { c as createServerFn } from "./index.mjs";
import { m as array, k as string } from "../_libs/zod.mjs";
import "../_chunks/_libs/react.mjs";
import "../_libs/section-matter.mjs";
import "../_libs/kind-of.mjs";
import "../_libs/extend-shallow.mjs";
import "../_libs/is-extendable.mjs";
import "../_chunks/_libs/js-yaml.mjs";
import "../_libs/strip-bom-string.mjs";
import "../_libs/fumadocs-mdx.mjs";
import "node:path";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_chunks/_libs/@tanstack/react-router.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
const getDocPagePath_createServerFn_handler = createServerRpc({
  id: "0e2e9791506fde9ff0566b84ac90e9be233606cd1ea59186bf1329ce07c3a948",
  name: "getDocPagePath",
  filename: "src/server/functions/docs.ts"
}, (opts, signal) => getDocPagePath.__executeServer(opts, signal));
const getDocPagePath = createServerFn({
  method: "GET"
}).inputValidator(array(string())).handler(getDocPagePath_createServerFn_handler, async ({
  data: slugs
}) => {
  const page = source.getPage(slugs);
  if (!page) return null;
  return {
    path: page.path
  };
});
const getDocContent_createServerFn_handler = createServerRpc({
  id: "aca4ff5d5ae420b220326d3c1e4fb29422560b6bde6f9bd946e1e91da7cf5bf8",
  name: "getDocContent",
  filename: "src/server/functions/docs.ts"
}, (opts, signal) => getDocContent.__executeServer(opts, signal));
const getDocContent = createServerFn({
  method: "GET"
}).inputValidator(string()).handler(getDocContent_createServerFn_handler, async ({
  data: slug
}) => {
  try {
    const basePath = path.join(process.cwd(), "src/routes/docs/-content");
    let filePath;
    if (!slug || slug === "") {
      filePath = path.join(basePath, "index.mdx");
    } else {
      const directPath = path.join(basePath, `${slug}.mdx`);
      const indexPath = path.join(basePath, slug, "index.mdx");
      if (fs.existsSync(directPath)) {
        filePath = directPath;
      } else if (fs.existsSync(indexPath)) {
        filePath = indexPath;
      } else {
        return null;
      }
    }
    const fileContent = fs.readFileSync(filePath, "utf-8");
    const {
      data: frontmatter,
      content
    } = matter(fileContent);
    return {
      title: frontmatter.title || "Documentation",
      description: frontmatter.description,
      content,
      slug
    };
  } catch (error) {
    console.error("Error loading doc:", error);
    return null;
  }
});
const getDocsList_createServerFn_handler = createServerRpc({
  id: "97411db5b6ef77f6bbbecf3179d1727fd19a9beb3945c4e41909cd8e7f080534",
  name: "getDocsList",
  filename: "src/server/functions/docs.ts"
}, (opts, signal) => getDocsList.__executeServer(opts, signal));
const getDocsList = createServerFn({
  method: "GET"
}).handler(getDocsList_createServerFn_handler, async () => {
  const basePath = path.join(process.cwd(), "src/routes/docs/-content");
  const docs = [];
  function scanDir(dir, prefix = "") {
    const files = fs.readdirSync(dir);
    for (const file of files) {
      const filePath = path.join(dir, file);
      const stat = fs.statSync(filePath);
      if (stat.isDirectory()) {
        scanDir(filePath, prefix ? `${prefix}/${file}` : file);
      } else if (file.endsWith(".mdx")) {
        const fileContent = fs.readFileSync(filePath, "utf-8");
        const {
          data: frontmatter
        } = matter(fileContent);
        let slug = prefix;
        if (file !== "index.mdx") {
          slug = prefix ? `${prefix}/${file.replace(".mdx", "")}` : file.replace(".mdx", "");
        }
        docs.push({
          slug,
          title: frontmatter.title || file.replace(".mdx", "")
        });
      }
    }
  }
  scanDir(basePath);
  return docs;
});
function nodeToString(node) {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (node === null || node === void 0) return "";
  if (Array.isArray(node)) return node.map(nodeToString).join("");
  if (typeof node === "object" && "props" in node) {
    return nodeToString(node.props?.children);
  }
  return String(node);
}
function serializePageTree(tree) {
  function serializeItem(item) {
    if (item.type === "separator") {
      return {
        type: "separator",
        name: nodeToString(item.name)
      };
    }
    if (item.type === "folder") {
      return {
        type: "folder",
        name: nodeToString(item.name),
        children: item.children?.map(serializeItem) ?? []
      };
    }
    return {
      type: "page",
      name: nodeToString(item.name),
      url: item.url
    };
  }
  return {
    name: nodeToString(tree.name),
    children: tree.children.map(serializeItem)
  };
}
const getPageTree_createServerFn_handler = createServerRpc({
  id: "d2612ee690868d7fd183ca6b9e838aa0a9d8efa4205f2509f510e420d8f45312",
  name: "getPageTree",
  filename: "src/server/functions/docs.ts"
}, (opts, signal) => getPageTree.__executeServer(opts, signal));
const getPageTree = createServerFn({
  method: "GET"
}).handler(getPageTree_createServerFn_handler, async () => {
  return serializePageTree(source.pageTree);
});
export {
  getDocContent_createServerFn_handler,
  getDocPagePath_createServerFn_handler,
  getDocsList_createServerFn_handler,
  getPageTree_createServerFn_handler
};
