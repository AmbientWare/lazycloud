from __future__ import annotations


class MissingDeploymentSettingError(RuntimeError):
    """A deployment fact that no default can supply is absent from the environment.

    Which database a process talks to, and which origin it is reached on, are
    decided by whoever deployed it. A settings class that answered with a local
    address instead would let the process run against whatever happens to be
    there, or hand that address to somebody else, and report success either way.
    So the absence is raised here, naming the variable, because that is the only
    thing an operator can act on.
    """

    def __init__(self, variable: str, *, purpose: str) -> None:
        self.variable = variable
        self.purpose = purpose
        super().__init__(
            f"{variable} is not set. It names {purpose}, which has no correct "
            "default: a process that fell back to a local address would use it "
            f"without saying so. Set {variable} in this process's environment."
        )


__all__ = ["MissingDeploymentSettingError"]
