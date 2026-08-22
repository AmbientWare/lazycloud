# Worker repository package

The trusted server-side bridge from authenticated worker HTTP contracts to
control-plane state, credential authorities, checkpoints, and image builds.

This is the control-plane half of the worker boundary and must never be installed
in or imported by a container worker. Worker payloads and the worker's client
stay with the worker; database, Redis, identity, control, and image adapters stay
here.

This is where the trust boundary is actually enforced. Establish assignment,
workspace, token, build, and capability authority before vending a credential or
mutating state. A worker's claim about what it is working on is an input, not a
fact. Never add a persistence fallback that lets a worker write around this
package.
