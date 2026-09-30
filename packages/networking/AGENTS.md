# Networking

- Own authenticated tunnels, dialing and forwarding; process composition stays out.
- Dial the supplied name first. Resolve/redial only after connect-time failures,
  before consuming the body; never replay a consumed stream.
- Certificates admit sessions; enrollment authorizes traffic. Recheck enrollment
  once per agent heartbeat and close streams on revocation.
- Existing streams may outlive certificate renewal. Close an expired session that
  did not renew; renewal preserves established streams.
