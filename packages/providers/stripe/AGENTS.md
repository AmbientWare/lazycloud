# Stripe Provider

Customer records and invoices for one Stripe account: where the money this
platform has already decided is owed actually moves.

- Stripe is the payment rail, not the pricing engine. This platform computes what
  is owed — rates, allowances, the management fee — and hands over finished
  amounts. Holding rates on both sides would decide the money twice from two
  catalogs that drift, and the figure the customer was charged would stop
  matching the figure this repository recorded.
- A rate limit and a rejected credential are retryable despite being 4xx. Neither
  is the caller's to fix, and treating either as terminal abandons an invoice a
  customer still owes.
- **Finalizing is irreversible.** A draft can be deleted; an issued invoice can
  only be voided, and it outlives the customer it was issued to — deleting the
  customer leaves it standing. Acceptance against a real account must therefore
  void what it issued, and must never finalize in live mode to find out what
  happens.
- Nothing here holds a price, a plan, or an allowance. An adapter that grew one
  would become a second answer to a question this platform already answers.
