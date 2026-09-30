# Web API schemas

- Mirror shared HTTP contracts exactly: lists, nullability, timestamps and omitted
  versus explicit-zero values. Export inferred types.
- No broad `z.any`, unknown payloads or unbounded record substitutes.
- Keep fetching, query state and formatting outside schemas; update producers and
  consumers together.
