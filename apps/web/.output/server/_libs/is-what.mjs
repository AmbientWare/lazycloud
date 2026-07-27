function getType(payload) {
  return Object.prototype.toString.call(payload).slice(8, -1);
}
function isArray(payload) {
  return getType(payload) === "Array";
}
function isPlainObject(payload) {
  if (getType(payload) !== "Object")
    return false;
  const prototype = Object.getPrototypeOf(payload);
  return !!prototype && prototype.constructor === Object && prototype === Object.prototype;
}
export {
  isPlainObject as a,
  isArray as i
};
