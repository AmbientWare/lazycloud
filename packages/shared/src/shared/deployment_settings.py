from __future__ import annotations


class MissingDeploymentSettingError(RuntimeError):
    """A deployment fact that no default can supply is absent from the environment.

    Which datastore a process talks to is decided by whoever deployed it. A
    settings class that answered with a local address instead would let a
    process reach whatever happens to answer there and report success, so the
    absence is raised here—naming the variable, because that is the only thing
    an operator can act on.
    """

    def __init__(self, variable: str, *, purpose: str) -> None:
        self.variable = variable
        self.purpose = purpose
        super().__init__(
            f"{variable} is not set. It selects {purpose}, which has no correct "
            "default: a process that fell back to a local address would read and "
            f"write whatever answers there without saying so. Set {variable} in "
            "this process's environment."
        )


__all__ = ["MissingDeploymentSettingError"]
