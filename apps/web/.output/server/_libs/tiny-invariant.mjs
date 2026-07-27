var prefix$1 = "Invariant failed";
function invariant$1(condition, message) {
  if (condition) {
    return;
  }
  {
    throw new Error(prefix$1);
  }
}
var prefix = "Invariant failed";
function invariant(condition, message) {
  {
    throw new Error(prefix);
  }
}
export {
  invariant as a,
  invariant$1 as i
};
