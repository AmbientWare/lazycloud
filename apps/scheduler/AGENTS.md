# Scheduler app

Scheduler settings, concrete adapter composition, and the process loop.

Scheduling decisions stay in `packages/scheduler`; this app chooses the concrete
adapters and runs the loop. Environment names here are part of the deployment
contract and change together with the deployment assets that set them.
