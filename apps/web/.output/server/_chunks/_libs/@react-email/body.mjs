import { r as reactExports, j as jsxRuntimeExports } from "../react.mjs";
var __defProp = Object.defineProperty;
var __defProps = Object.defineProperties;
var __getOwnPropDescs = Object.getOwnPropertyDescriptors;
var __getOwnPropSymbols = Object.getOwnPropertySymbols;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __propIsEnum = Object.prototype.propertyIsEnumerable;
var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
var __spreadValues = (a, b) => {
  for (var prop in b || (b = {}))
    if (__hasOwnProp.call(b, prop))
      __defNormalProp(a, prop, b[prop]);
  if (__getOwnPropSymbols)
    for (var prop of __getOwnPropSymbols(b)) {
      if (__propIsEnum.call(b, prop))
        __defNormalProp(a, prop, b[prop]);
    }
  return a;
};
var __spreadProps = (a, b) => __defProps(a, __getOwnPropDescs(b));
var __objRest = (source, exclude) => {
  var target = {};
  for (var prop in source)
    if (__hasOwnProp.call(source, prop) && exclude.indexOf(prop) < 0)
      target[prop] = source[prop];
  if (source != null && __getOwnPropSymbols)
    for (var prop of __getOwnPropSymbols(source)) {
      if (exclude.indexOf(prop) < 0 && __propIsEnum.call(source, prop))
        target[prop] = source[prop];
    }
  return target;
};
var Body = reactExports.forwardRef(
  (_a, ref) => {
    var _b = _a, { children, style } = _b, props = __objRest(_b, ["children", "style"]);
    return /* @__PURE__ */ jsxRuntimeExports.jsx(
      "body",
      __spreadProps(__spreadValues({}, props), {
        style: {
          background: style == null ? void 0 : style.background,
          backgroundColor: style == null ? void 0 : style.backgroundColor
        },
        ref,
        children: /* @__PURE__ */ jsxRuntimeExports.jsx(
          "table",
          {
            border: 0,
            width: "100%",
            cellPadding: "0",
            cellSpacing: "0",
            role: "presentation",
            align: "center",
            children: /* @__PURE__ */ jsxRuntimeExports.jsx("tbody", { children: /* @__PURE__ */ jsxRuntimeExports.jsx("tr", { children: /* @__PURE__ */ jsxRuntimeExports.jsx("td", { style, children }) }) })
          }
        )
      })
    );
  }
);
Body.displayName = "Body";
export {
  Body as B
};
