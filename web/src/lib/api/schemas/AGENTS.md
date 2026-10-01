# Web API schemas

- These Zod schemas describe two things only: operations no merged packet serves
  yet (their callers use `lib/api/unserved.ts`), and values the public contract
  leaves open, such as a function's client contract.
- When an area moves to the generated types, delete its schemas here.
- Keep fetching, query state and formatting outside schemas.
