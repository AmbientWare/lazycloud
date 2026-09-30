# Python test helpers

- Hold fixtures shared by the Python packages' owner tests. Package tests live
  beside their package; platform acceptance across owners does not belong here.
- Keep checks independent of backend imports. Language-neutral wire examples live
  in the root contracts directory.
- Isolate credentials, configuration, mutable globals and temporary resources.
  Tests must not inherit another checkout's environment or contact real accounts.
