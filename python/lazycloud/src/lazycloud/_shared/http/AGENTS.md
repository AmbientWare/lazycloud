# HTTP contracts

- Use one precise `HttpModel` per wire payload: `datetime`, closed enums,
  `{data, next}` lists, explicit byte encoding and validators for invariants.
- Failures use HTTP status and the shared error response; per-event stream status
  is domain data.
- Update producers and all SDK/CLI, runner and web consumers together. Delete
  unconsumed models.
