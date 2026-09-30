# Administration CLI

- Own internal `lazycloud-admin`; reuse public CLI behavior from the SDK.
- Platform initialization uses offline authority, without an account token.
- Fleet destruction stops admission and schedulers first, targets platform units
  only, and retains ownership records until provider cleanup is confirmed.
- Machine join uses the public SDK; operator unit commands stay platform-scoped.
