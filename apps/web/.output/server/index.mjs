globalThis.__nitro_main__ = import.meta.url;
import { a as FastResponse, s as serve } from "./_libs/srvx.mjs";
import { d as defineHandler, H as HTTPError, t as toEventHandler, a as defineLazyEventHandler, b as H3Core, c as toRequest } from "./_libs/h3.mjs";
import { d as decodePath, w as withLeadingSlash, a as withoutTrailingSlash, j as joinURL } from "./_libs/ufo.mjs";
import { promises } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import "./_libs/rou3.mjs";
function lazyService(loader) {
  let promise, mod;
  return {
    fetch(req) {
      if (mod) {
        return mod.fetch(req);
      }
      if (!promise) {
        promise = loader().then((_mod) => mod = _mod.default || _mod);
      }
      return promise.then((mod2) => mod2.fetch(req));
    }
  };
}
const services = {
  ["ssr"]: lazyService(() => import("./_ssr/index.mjs"))
};
globalThis.__nitro_vite_envs__ = services;
const errorHandler$1 = (error, event) => {
  const res = defaultHandler(error, event);
  return new FastResponse(typeof res.body === "string" ? res.body : JSON.stringify(res.body, null, 2), res);
};
function defaultHandler(error, event, opts) {
  const isSensitive = error.unhandled;
  const status = error.status || 500;
  const url = event.url || new URL(event.req.url);
  if (status === 404) {
    const baseURL = "/";
    if (/^\/[^/]/.test(baseURL) && !url.pathname.startsWith(baseURL)) {
      const redirectTo = `${baseURL}${url.pathname.slice(1)}${url.search}`;
      return {
        status: 302,
        statusText: "Found",
        headers: { location: redirectTo },
        body: `Redirecting...`
      };
    }
  }
  if (isSensitive && !opts?.silent) {
    const tags = [error.unhandled && "[unhandled]"].filter(Boolean).join(" ");
    console.error(`[request error] ${tags} [${event.req.method}] ${url}
`, error);
  }
  const headers2 = {
    "content-type": "application/json",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "script-src 'none'; frame-ancestors 'none';"
  };
  if (status === 404 || !event.res.headers.has("cache-control")) {
    headers2["cache-control"] = "no-cache";
  }
  const body = {
    error: true,
    url: url.href,
    status,
    statusText: error.statusText,
    message: isSensitive ? "Server Error" : error.message,
    data: isSensitive ? void 0 : error.data
  };
  return {
    status,
    statusText: error.statusText,
    headers: headers2,
    body
  };
}
const errorHandlers = [errorHandler$1];
async function errorHandler(error, event) {
  for (const handler of errorHandlers) {
    try {
      const response = await handler(error, event, { defaultHandler });
      if (response) {
        return response;
      }
    } catch (error2) {
      console.error(error2);
    }
  }
}
const headers = ((m) => function headersRouteRule(event) {
  for (const [key2, value] of Object.entries(m.options || {})) {
    event.res.headers.set(key2, value);
  }
});
const assets = {
  "/assets/textarea-CDKqm1o4.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"269-2g3DTRAbo5azzqrqwJsnPzpjveg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 617,
    "path": "../public/assets/textarea-CDKqm1o4.js"
  },
  "/assets/_authenticated-BPa-oM6L.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"3207-B15LfJIzuhMcMpWa2JPFYUQBJkU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 12807,
    "path": "../public/assets/_authenticated-BPa-oM6L.js"
  },
  "/assets/success-C0N5yFYk.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"4ce-EuU/pxwknL1RuLQhjKC+xgALuBs"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1230,
    "path": "../public/assets/success-C0N5yFYk.js"
  },
  "/assets/useRouteUser-Cvr3cUFJ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"bc-Pvq+2PIgxN6txIYbdT1IMvcT99s"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 188,
    "path": "../public/assets/useRouteUser-Cvr3cUFJ.js"
  },
  "/assets/builds-D6hz8iak.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"5d24-cC0QEkPasAkNgCrG7Ztq1j7ABrM"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 23844,
    "path": "../public/assets/builds-D6hz8iak.js"
  },
  "/assets/rollback-DD5oPHMW.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"18b5-p/07u37i79qIozLMt3flSs7iYyE"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 6325,
    "path": "../public/assets/rollback-DD5oPHMW.js"
  },
  "/assets/proxy-CRp4I0cu.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1c35f-C4hgkfBpSaqIG9Jby5f9c2tB5Bs"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 115551,
    "path": "../public/assets/proxy-CRp4I0cu.js"
  },
  "/assets/route-BmEY1pS6.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1e4d-I5+cROL7d2Elg+basvLWuU5QDy4"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 7757,
    "path": "../public/assets/route-BmEY1pS6.js"
  },
  "/assets/check-DSkrsKiy.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"7c-YwdBp4jvutKm1g0WjiMEgxwoRyQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 124,
    "path": "../public/assets/check-DSkrsKiy.js"
  },
  "/assets/terms-D4AKldWm.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"99b5-8RsSiJ2zcXWTSGAmCOJu+eT428M"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 39349,
    "path": "../public/assets/terms-D4AKldWm.js"
  },
  "/assets/destroy-CPh0iNsn.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1706-CNV9OLxAKJRza9JCWcHACp1mRZw"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 5894,
    "path": "../public/assets/destroy-CPh0iNsn.js"
  },
  "/assets/networking-BVNBGO6G.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"87fb-QnyjZOlJaGetWa4WB9w1gKoHkB8"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 34811,
    "path": "../public/assets/networking-BVNBGO6G.js"
  },
  "/assets/mixedbread-B4x0e8AJ-D5iTKtKX.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2e84-5sALQcp7hiD7C7ZskqMYfuvTLLU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 11908,
    "path": "../public/assets/mixedbread-B4x0e8AJ-D5iTKtKX.js"
  },
  "/assets/activity-Ca5aJz84.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"ea-QGFRzsMVICDXlfklPQaDOy/QWxw"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 234,
    "path": "../public/assets/activity-Ca5aJz84.js"
  },
  "/assets/label-CDRKxZe0.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"c20-8cyFf0taGhQpWJ4/elQkCb7aiHE"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 3104,
    "path": "../public/assets/label-CDRKxZe0.js"
  },
  "/assets/volumes-CFNBKTqY.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"3108-Tn6AYU9YAq7jj3idxq+eRWO4aTs"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 12552,
    "path": "../public/assets/volumes-CFNBKTqY.js"
  },
  "/assets/acceptable-use-CrBW5yRZ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2b61-TB0YPRnXsvMNnUy1ABuEpxomv/o"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 11105,
    "path": "../public/assets/acceptable-use-CrBW5yRZ.js"
  },
  "/assets/badge-BsuzWfe_.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"478-BWcfKrK09PiSEcs0vYNA07eE9Nk"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1144,
    "path": "../public/assets/badge-BsuzWfe_.js"
  },
  "/install.sh": {
    "type": "application/x-sh",
    "etag": '"11fb-Fnab4Int7uXwynmpMtPPgbL4pJA"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 4603,
    "path": "../public/install.sh"
  },
  "/robots.txt": {
    "type": "text/plain; charset=utf-8",
    "etag": '"43-BEzmj4PuhUNHX+oW9uOnPSihxtU"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 67,
    "path": "../public/robots.txt"
  },
  "/manifest.json": {
    "type": "application/json",
    "etag": '"25c-KGyFu5wKEWSL/bjikpfR5Cc18hA"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 604,
    "path": "../public/manifest.json"
  },
  "/install.ps1": {
    "type": "text/plain; charset=utf-8",
    "etag": '"1110-MT0S2nAj42EvkZTmgtAEOj11PSk"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 4368,
    "path": "../public/install.ps1"
  },
  "/favicon.ico": {
    "type": "image/vnd.microsoft.icon",
    "etag": '"3c2e-mg+pQCDxIpILyCcz0lsglQ5whdk"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 15406,
    "path": "../public/favicon.ico"
  },
  "/lazycloud.png": {
    "type": "image/png",
    "etag": '"1ae67-q2xvQf3EMQj7lcOc0In3MG5ODqc"',
    "mtime": "2026-01-26T07:31:35.856Z",
    "size": 110183,
    "path": "../public/lazycloud.png"
  },
  "/assets/usage-i5J7n70k.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"64943-ln6dVyHBK+xV33Eq1XWj1EICsww"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 411971,
    "path": "../public/assets/usage-i5J7n70k.js"
  },
  "/assets/input-DyvP4ks8.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"3d2-7w/+gXjDTdW7wcNkW5sGjjeNKLY"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 978,
    "path": "../public/assets/input-DyvP4ks8.js"
  },
  "/assets/deployments-Bnkzyyxs.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"164b-z4MOwNkW9syt2tyvQaax95uzezg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 5707,
    "path": "../public/assets/deployments-Bnkzyyxs.js"
  },
  "/assets/rolldown_runtime-DEElWEPh.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"181-EZACFBk4/EgoLUnSo0pUcyqtdWk"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 385,
    "path": "../public/assets/rolldown_runtime-DEElWEPh.js"
  },
  "/assets/index-Bf3fJ_RS.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1a7-hMxEheHXS62NjEhIht+lhzZ9msc"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 423,
    "path": "../public/assets/index-Bf3fJ_RS.js"
  },
  "/assets/index-Cpf6Sn9S.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"7105-yrUwSo/hAHPxVEOiaopwGNuYf/A"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 28933,
    "path": "../public/assets/index-Cpf6Sn9S.js"
  },
  "/assets/algolia-IT2aRQE6-CW1WFCSJ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"30c-pmgVmEwaGCC4c/R3tIcfM4Sj4Dg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 780,
    "path": "../public/assets/algolia-IT2aRQE6-CW1WFCSJ.js"
  },
  "/assets/chevron-right-DeQfv4Cw.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"bf-gVgRgmknjqQ/UyqEN3xaNZiQrFU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 191,
    "path": "../public/assets/chevron-right-DeQfv4Cw.js"
  },
  "/assets/chevron-up-Bt3vDpmR.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"80-Ti3ihryZd5+8YK7tfAo2P4UJOmM"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 128,
    "path": "../public/assets/chevron-up-Bt3vDpmR.js"
  },
  "/assets/api-enov3vYN.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"5e-6lkEXoisExdCsBlEOTdbC36tv6g"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 94,
    "path": "../public/assets/api-enov3vYN.js"
  },
  "/assets/llm-chatbot-Cg4jEgFv.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"7910-omkOZTFwbmeXf19prb/sZepwYhg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 30992,
    "path": "../public/assets/llm-chatbot-Cg4jEgFv.js"
  },
  "/assets/_landing-BFbDXtOe.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"5023-PvB34+LNyx22RB6Nt48pASL3IRA"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 20515,
    "path": "../public/assets/_landing-BFbDXtOe.js"
  },
  "/assets/usage-DHQdmilO.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"d5c-OMh7N4TbzQQguv1VHO4Axz5d/AM"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 3420,
    "path": "../public/assets/usage-DHQdmilO.js"
  },
  "/assets/resources-BgjloXlt.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"211d-rKc1nLfLYWXbE5PvjaUQ6a2+3S4"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 8477,
    "path": "../public/assets/resources-BgjloXlt.js"
  },
  "/assets/index-BrnpUVsb.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"148d-r35Onrv0zhBkE+j5Q1M3KqJNjbA"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 5261,
    "path": "../public/assets/index-BrnpUVsb.js"
  },
  "/assets/scaling-8WOaGiAP.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2757-GC1fXYYnJmVUqszAH2rCxLnTjJc"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 10071,
    "path": "../public/assets/scaling-8WOaGiAP.js"
  },
  "/assets/orama-cloud-legacy-DG0asrcn-BcD80i3E.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"4ab-mvbf5RmHPPqPXTFbiLVeHOPE6S0"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1195,
    "path": "../public/assets/orama-cloud-legacy-DG0asrcn-BcD80i3E.js"
  },
  "/assets/index-wEE2YFcK.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"154a-fnYKSelS2/FQDNfOcvJgK7yCkiU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 5450,
    "path": "../public/assets/index-wEE2YFcK.js"
  },
  "/assets/styles-M8h1iuKo.css": {
    "type": "text/css; charset=utf-8",
    "etag": '"2ce14-bWh15uH2soul6l5ORvECsfBqKmU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 183828,
    "path": "../public/assets/styles-M8h1iuKo.css"
  },
  "/assets/search-default-Dxb6UTm9.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"355b-MOxwWsb2D2y0WszivhC1MfBSaVU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 13659,
    "path": "../public/assets/search-default-Dxb6UTm9.js"
  },
  "/assets/stock-dashboard-D9G0i48q.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"4e94-6qBFSQCWpCiAGUBXzVokixiRqiQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 20116,
    "path": "../public/assets/stock-dashboard-D9G0i48q.js"
  },
  "/assets/cicd-DHsqMCtC.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"7efa-ANsWTYzQ4gfw7qx+Zwb0KdN3VZo"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 32506,
    "path": "../public/assets/cicd-DHsqMCtC.js"
  },
  "/assets/workspaces-B-InNWVJ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"f073-rXZetRaWKPnzAnH+xDELRVQDNJk"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 61555,
    "path": "../public/assets/workspaces-B-InNWVJ.js"
  },
  "/assets/pricing-ARugjHtb.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"204ed-n2tbZeYnoJWUUsxorenqloPiQqk"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 132333,
    "path": "../public/assets/pricing-ARugjHtb.js"
  },
  "/assets/main-DJ9jhRmH.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"ab636-o1lHGAuTyfwaSOsMT+gW9BxDr6M"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 702006,
    "path": "../public/assets/main-DJ9jhRmH.js"
  },
  "/assets/fetch-B9AxBORJ-BXhSS5YA.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1a0-YxUSDvvrpQzEtggJeyvfGNz+bSc"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 416,
    "path": "../public/assets/fetch-B9AxBORJ-BXhSS5YA.js"
  },
  "/assets/constants-aVpi1d7C.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"53-BKFgoRWN68RUajSHYgWmG6iRD/4"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 83,
    "path": "../public/assets/constants-aVpi1d7C.js"
  },
  "/assets/sidebar-DKdzvzbj.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"23c5-hvI0h8HgV+CyZ9m/QxScNqj34EU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 9157,
    "path": "../public/assets/sidebar-DKdzvzbj.js"
  },
  "/assets/button-BUyujdYW.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"636-cnQufG1Ef0+DE6VILcHtBQ9WBg0"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1590,
    "path": "../public/assets/button-BUyujdYW.js"
  },
  "/assets/index-CYBj12tm.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2a26-WTrt4hmBx97XMSZ6+u9JqO/xJEE"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 10790,
    "path": "../public/assets/index-CYBj12tm.js"
  },
  "/assets/scaling-C0v5o4qv.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"5365-cTC+JQDUo8SXbAEBFdY8TBknzTQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 21349,
    "path": "../public/assets/scaling-C0v5o4qv.js"
  },
  "/assets/file-text-VemFx9Qp.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"181-SxtPy6pZQrOb6OkEDrxyye57Ntc"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 385,
    "path": "../public/assets/file-text-VemFx9Qp.js"
  },
  "/assets/volume-CrhzXT-b.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"45ab-whs7H4qM1K7FgTHXS3V45f3CcYg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 17835,
    "path": "../public/assets/volume-CrhzXT-b.js"
  },
  "/assets/dashboard-xbDTEic6.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"148f-EJlExdc/Qm5TkW6P4CIjebKKftk"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 5263,
    "path": "../public/assets/dashboard-xbDTEic6.js"
  },
  "/assets/styled-tooltip-CSHugHOy.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"36f-QkWS1sc+SSC53+q9DazeTsFd7qw"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 879,
    "path": "../public/assets/styled-tooltip-CSHugHOy.js"
  },
  "/assets/search-CBAFoCmV-qJr9DjGl.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"29a-uFHREuil+6jdbsB+l7uB+jhAsm8"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 666,
    "path": "../public/assets/search-CBAFoCmV-qJr9DjGl.js"
  },
  "/assets/_-D5st3Ta9.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1e4-7inMsjY7wzj3vP1e1/kZSGQo8G0"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 484,
    "path": "../public/assets/_-D5st3Ta9.js"
  },
  "/assets/CurrentYear-bUIXM8jp.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"bc8-dBdz8gmcMTVb1t4VX/MoSG6srAc"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 3016,
    "path": "../public/assets/CurrentYear-bUIXM8jp.js"
  },
  "/assets/remove-undefined-CiwokjLP-oajMeTFk.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"d7-wlj5j7tDxBiCdXw6SHXSAKXRq3w"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 215,
    "path": "../public/assets/remove-undefined-CiwokjLP-oajMeTFk.js"
  },
  "/assets/secrets-Cpkpi8b_.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"6165-Wv09Zl+hlYP7BV8hMM3a8eZehPw"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 24933,
    "path": "../public/assets/secrets-Cpkpi8b_.js"
  },
  "/assets/init-DWVlrOCX.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1ef0-ma8k+HgLqQmktvufXqK9urWyRLM"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 7920,
    "path": "../public/assets/init-DWVlrOCX.js"
  },
  "/assets/callback-DrrM-NvG.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2d6-05+YsBDtej1MFv5d1XPJ6gZnyGE"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 726,
    "path": "../public/assets/callback-DrrM-NvG.js"
  },
  "/assets/x-BMPQx-cJ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"9a-0ec05miBdT36byO4V4r5ppYTXiI"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 154,
    "path": "../public/assets/x-BMPQx-cJ.js"
  },
  "/assets/external-link-9gqgGiUt.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"fb-ii7HqXi8xGUYCJurRq42SVKBU+Q"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 251,
    "path": "../public/assets/external-link-9gqgGiUt.js"
  },
  "/assets/how-it-works-B2Hqv8TO.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"4625-mWaOAqkUQFs1YjJ4XXX+jNljbhs"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 17957,
    "path": "../public/assets/how-it-works-B2Hqv8TO.js"
  },
  "/assets/support-KPlDAZLE.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"557-FT8j1pmeLxp8kMPGJ3SOJEBlO30"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1367,
    "path": "../public/assets/support-KPlDAZLE.js"
  },
  "/assets/subscribe-DK-rQqXQ.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"564-HY1uv+PWmnzzc1wZ7hbeKH4STPQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1380,
    "path": "../public/assets/subscribe-DK-rQqXQ.js"
  },
  "/assets/static-Dg4_dKaf-BYLZHFm9.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"f99d-15+HElWr5tl3mzQJ0bo2k2cTk6g"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 63901,
    "path": "../public/assets/static-Dg4_dKaf-BYLZHFm9.js"
  },
  "/assets/_--60sLQh5.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"1a7-UtC7EhNrOuHN9iRinB3dNQGmRWo"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 423,
    "path": "../public/assets/_--60sLQh5.js"
  },
  "/assets/shield-D6UhvHji.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"110-HAseP7vTMVP48isE2YmPS72gUyg"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 272,
    "path": "../public/assets/shield-D6UhvHji.js"
  },
  "/assets/orama-cloud-CiI54EU9-B9Eol4rv.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"4cb-rKmBZlZiSLJcIsjs0lZYk1+dkdU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 1227,
    "path": "../public/assets/orama-cloud-CiI54EU9-B9Eol4rv.js"
  },
  "/assets/index-DZbhBkfW.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"23b9-dZqRyz7d/AtjjH4hTRfaNZbspP0"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 9145,
    "path": "../public/assets/index-DZbhBkfW.js"
  },
  "/assets/login-Lx8sWx-4.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"227-J+gRYZ64X76HsViu2mVjF3wYyiw"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 551,
    "path": "../public/assets/login-Lx8sWx-4.js"
  },
  "/assets/deploy-jtX9OD9U.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"21c9-wpjHkITNXthRxHjzSsPqfNNqH60"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 8649,
    "path": "../public/assets/deploy-jtX9OD9U.js"
  },
  "/assets/security-C74-1Efy.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"21df-sP2IkEOWpc60olOaoi9nI4piQVM"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 8671,
    "path": "../public/assets/security-C74-1Efy.js"
  },
  "/assets/expand-confirm-button-BgR5Nsks.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"ac56-AWtIMw3/RULOwFlaeDYUPZPSvWU"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 44118,
    "path": "../public/assets/expand-confirm-button-BgR5Nsks.js"
  },
  "/assets/image-transformer-D2xZ0Qmj.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"66fb-3MYvtvEv2zrUdx3Gc0bx0Geu4Q0"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 26363,
    "path": "../public/assets/image-transformer-D2xZ0Qmj.js"
  },
  "/assets/service-D5BYMp8W.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"2860-+3CSDOehN4s1xMLHKGB7r5mE/jA"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 10336,
    "path": "../public/assets/service-D5BYMp8W.js"
  },
  "/assets/privacy-YaBXEV_5.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"6811-v0uGks1/6VNL4mqTROGTOM8ZH5k"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 26641,
    "path": "../public/assets/privacy-YaBXEV_5.js"
  },
  "/assets/chevrons-up-down-DWPzwPIG.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"ae-lppXYaPd27PN9iEp3Onc2Ikv+ss"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 174,
    "path": "../public/assets/chevrons-up-down-DWPzwPIG.js"
  },
  "/assets/refresh-cw-BQYy3Shn.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"141-zwb5nnlPa9OsAY897dt2E771DiQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 321,
    "path": "../public/assets/refresh-cw-BQYy3Shn.js"
  },
  "/assets/workspaces-Coo6R8ww.js": {
    "type": "text/javascript; charset=utf-8",
    "etag": '"20d7-fW5nA0tJQGyWD3G1XrJt5+YalYQ"',
    "mtime": "2026-01-26T07:31:36.269Z",
    "size": 8407,
    "path": "../public/assets/workspaces-Coo6R8ww.js"
  }
};
function readAsset(id) {
  const serverDir = dirname(fileURLToPath(globalThis.__nitro_main__));
  return promises.readFile(resolve(serverDir, assets[id].path));
}
const publicAssetBases = {};
function isPublicAssetURL(id = "") {
  if (assets[id]) {
    return true;
  }
  for (const base in publicAssetBases) {
    if (id.startsWith(base)) {
      return true;
    }
  }
  return false;
}
function getAsset(id) {
  return assets[id];
}
const METHODS = /* @__PURE__ */ new Set(["HEAD", "GET"]);
const EncodingMap = {
  gzip: ".gz",
  br: ".br"
};
const _jFCTyu = defineHandler((event) => {
  if (event.req.method && !METHODS.has(event.req.method)) {
    return;
  }
  let id = decodePath(withLeadingSlash(withoutTrailingSlash(event.url.pathname)));
  let asset;
  const encodingHeader = event.req.headers.get("accept-encoding") || "";
  const encodings = [...encodingHeader.split(",").map((e) => EncodingMap[e.trim()]).filter(Boolean).sort(), ""];
  if (encodings.length > 1) {
    event.res.headers.append("Vary", "Accept-Encoding");
  }
  for (const encoding of encodings) {
    for (const _id of [id + encoding, joinURL(id, "index.html" + encoding)]) {
      const _asset = getAsset(_id);
      if (_asset) {
        asset = _asset;
        id = _id;
        break;
      }
    }
  }
  if (!asset) {
    if (isPublicAssetURL(id)) {
      event.res.headers.delete("Cache-Control");
      throw new HTTPError({ status: 404 });
    }
    return;
  }
  const ifNotMatch = event.req.headers.get("if-none-match") === asset.etag;
  if (ifNotMatch) {
    event.res.status = 304;
    event.res.statusText = "Not Modified";
    return "";
  }
  const ifModifiedSinceH = event.req.headers.get("if-modified-since");
  const mtimeDate = new Date(asset.mtime);
  if (ifModifiedSinceH && asset.mtime && new Date(ifModifiedSinceH) >= mtimeDate) {
    event.res.status = 304;
    event.res.statusText = "Not Modified";
    return "";
  }
  if (asset.type) {
    event.res.headers.set("Content-Type", asset.type);
  }
  if (asset.etag && !event.res.headers.has("ETag")) {
    event.res.headers.set("ETag", asset.etag);
  }
  if (asset.mtime && !event.res.headers.has("Last-Modified")) {
    event.res.headers.set("Last-Modified", mtimeDate.toUTCString());
  }
  if (asset.encoding && !event.res.headers.has("Content-Encoding")) {
    event.res.headers.set("Content-Encoding", asset.encoding);
  }
  if (asset.size > 0 && !event.res.headers.has("Content-Length")) {
    event.res.headers.set("Content-Length", asset.size.toString());
  }
  return readAsset(id);
});
const findRouteRules = /* @__PURE__ */ (() => {
  const $0 = [{ name: "headers", route: "/assets/**", handler: headers, options: { "cache-control": "public, max-age=31536000, immutable" } }];
  return (m, p) => {
    let r = [];
    if (p.charCodeAt(p.length - 1) === 47) p = p.slice(0, -1) || "/";
    let s = p.split("/");
    s.length - 1;
    if (s[1] === "assets") {
      r.unshift({ data: $0, params: { "_": s.slice(2).join("/") } });
    }
    return r;
  };
})();
const _lazy_66l29W = defineLazyEventHandler(() => Promise.resolve().then(function() {
  return ssrRenderer$1;
}));
const findRoute = /* @__PURE__ */ (() => {
  const data = { route: "/**", handler: _lazy_66l29W };
  return ((_m, p) => {
    return { data, params: { "_": p.slice(1) } };
  });
})();
const globalMiddleware = [
  toEventHandler(_jFCTyu)
].filter(Boolean);
const APP_ID = "default";
function useNitroApp() {
  let instance = useNitroApp._instance;
  if (instance) {
    return instance;
  }
  instance = useNitroApp._instance = createNitroApp();
  globalThis.__nitro__ = globalThis.__nitro__ || {};
  globalThis.__nitro__[APP_ID] = instance;
  return instance;
}
function createNitroApp() {
  const hooks = void 0;
  const captureError = (error, errorCtx) => {
    if (errorCtx?.event) {
      const errors = errorCtx.event.req.context?.nitro?.errors;
      if (errors) {
        errors.push({
          error,
          context: errorCtx
        });
      }
    }
  };
  const h3App = createH3App({ onError(error, event) {
    return errorHandler(error, event);
  } });
  let appHandler = (req) => {
    req.context ||= {};
    req.context.nitro = req.context.nitro || { errors: [] };
    return h3App.fetch(req);
  };
  const app = {
    fetch: appHandler,
    h3: h3App,
    hooks,
    captureError
  };
  return app;
}
function createH3App(config) {
  const h3App = new H3Core(config);
  h3App["~findRoute"] = (event) => findRoute(event.req.method, event.url.pathname);
  h3App["~middleware"].push(...globalMiddleware);
  {
    h3App["~getMiddleware"] = (event, route) => {
      const pathname = event.url.pathname;
      const method = event.req.method;
      const middleware = [];
      {
        const routeRules = getRouteRules(method, pathname);
        event.context.routeRules = routeRules?.routeRules;
        if (routeRules?.routeRuleMiddleware.length) {
          middleware.push(...routeRules.routeRuleMiddleware);
        }
      }
      middleware.push(...h3App["~middleware"]);
      if (route?.data?.middleware?.length) {
        middleware.push(...route.data.middleware);
      }
      return middleware;
    };
  }
  return h3App;
}
function getRouteRules(method, pathname) {
  const m = findRouteRules(method, pathname);
  if (!m?.length) {
    return { routeRuleMiddleware: [] };
  }
  const routeRules = {};
  for (const layer of m) {
    for (const rule of layer.data) {
      const currentRule = routeRules[rule.name];
      if (currentRule) {
        if (rule.options === false) {
          delete routeRules[rule.name];
          continue;
        }
        if (typeof currentRule.options === "object" && typeof rule.options === "object") {
          currentRule.options = {
            ...currentRule.options,
            ...rule.options
          };
        } else {
          currentRule.options = rule.options;
        }
        currentRule.route = rule.route;
        currentRule.params = {
          ...currentRule.params,
          ...layer.params
        };
      } else if (rule.options !== false) {
        routeRules[rule.name] = {
          ...rule,
          params: layer.params
        };
      }
    }
  }
  const middleware = [];
  for (const rule of Object.values(routeRules)) {
    if (rule.options === false || !rule.handler) {
      continue;
    }
    middleware.push(rule.handler(rule));
  }
  return {
    routeRules,
    routeRuleMiddleware: middleware
  };
}
function _captureError(error, type) {
  console.error(`[${type}]`, error);
  useNitroApp().captureError?.(error, { tags: [type] });
}
function trapUnhandledErrors() {
  process.on("unhandledRejection", (error) => _captureError(error, "unhandledRejection"));
  process.on("uncaughtException", (error) => _captureError(error, "uncaughtException"));
}
const port = Number.parseInt(process.env.NITRO_PORT || process.env.PORT || "") || 3e3;
const host = process.env.NITRO_HOST || process.env.HOST;
const cert = process.env.NITRO_SSL_CERT;
const key = process.env.NITRO_SSL_KEY;
const nitroApp = useNitroApp();
let _fetch = nitroApp.fetch;
serve({
  port,
  hostname: host,
  tls: cert && key ? {
    cert,
    key
  } : void 0,
  fetch: _fetch,
  bun: { websocket: void 0 }
});
trapUnhandledErrors();
const bun = {};
function fetchViteEnv(viteEnvName, input, init) {
  const envs = globalThis.__nitro_vite_envs__ || {};
  const viteEnv = envs[viteEnvName];
  if (!viteEnv) {
    throw HTTPError.status(404);
  }
  return Promise.resolve(viteEnv.fetch(toRequest(input, init)));
}
function ssrRenderer({ req }) {
  return fetchViteEnv("ssr", req);
}
const ssrRenderer$1 = /* @__PURE__ */ Object.freeze({
  __proto__: null,
  default: ssrRenderer
});
export {
  bun as default
};
