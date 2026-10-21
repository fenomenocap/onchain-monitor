# Liquidity management methodology

This is the playbook behind On-chain Monitor: how a token treasury's
liquidity is run, and how each decision shows up in the code. The software is how the
method is enforced. Every rule below is something the system computes, checks or
refuses to guess.

No figures from any real book appear here. The method is the point.

---

## 1. The job

A token treasury provides liquidity for its holders' sake: so the token can be
bought and sold without punishing slippage, on the venues where people trade it.
That liquidity is capital, and capital has to answer four questions:

1. **Is it doing its job?** Depth and tight markets where holders need them.
2. **Is it paying for itself?** Fees earned against what providing liquidity
   costs: impermanent loss, swap costs and gas.
3. **Is it within what we agreed?** Concentration, loss, range and counterparty
   limits approved in advance, not invented after the fact.
4. **Can anyone check it?** Every number traceable to on-chain events, priced
   when they happened.

Two books are managed against those questions:

- **On-chain:** protocol-owned liquidity, in concentrated-liquidity pools across
  several chains and DEXes, held in treasury wallets and executed from a multisig.
- **Off-chain:** market makers that borrow inventory or hold options in exchange
  for quoting the token on centralised exchanges.

They serve the same holders, so they are run as one desk.

## 2. Measure before acting

A liquidity decision is only as good as the book it is made from, so accounting
comes first.

- **Event-time cost basis.** Every deposit, withdrawal and fee claim is stored once,
  priced in USD at its own block, and never rewritten. Cost basis cannot drift
  because a price feed changed later.
- **The right benchmark.** Performance is measured against *holding the tokens
  actually deposited*. LP value minus that hold value is impermanent loss. Fees
  have to beat it for the position to have been worth running.
- **Flows netted out.** Period P&L (day, week, month, quarter, year, inception)
  removes deposits and withdrawals, so adding capital never reads as profit. Each
  period splits into market move, impermanent loss, fees earned and gas.
- **Accrual, not cash.** Fees earned = fees collected + change in unclaimed fees.
  A month-start harvest that sweeps the previous month's accrual is booked to the
  month that earned it. Harvests are recognised by coverage (a sweep across the
  book), not by size, so a stray dust claim cannot reset the clock.
- **Gas is a cost line.** Read from receipts, priced at the block, counted once
  per transaction however many positions it touched.

## 3. Choosing a range

For each pool, the optimiser solves for the band that maximises **expected fees minus expected
impermanent loss** over a horizon. The model is deliberately simple and every
piece is closed-form, so the reasoning can be checked:

- **Price** is a driftless random walk in log terms. Volatility σ is measured
  from the pool's own daily history and carried with its 95% confidence band.
  Too little history means "not enough history", not a guess.
- **Occupancy, not first exit.** A position is left untouched inside the horizon, so
  what matters is the expected number of days in range: a price that leaves and
  comes back earns again.
- **Fee share saturates.** A narrower band concentrates more liquidity behind the
  same capital and wins a larger share of fees, until our liquidity dominates the
  pool. Past that point, narrowing buys almost nothing and costs more loss.
- **Impermanent loss accrues while in range** at a rate set by liquidity, price
  and σ². Out of range, a position just holds one token and accrues none.
- **The book's view.** Fees one of the book's positions takes from another of its
  positions in the same pool are not a gain. Only fees won from other LPs count.
- **Sizing.** Setting marginal fee against marginal loss gives the optimal
  *liquidity* for a pool independent of band width; the band decides how much
  capital that liquidity takes.
- **Volume responds to depth.** Thinner pools lose aggregator flow. That
  response (elasticity ε) is measured from each pool's own history: swap logs where
  available, the pool's fee counter where they aren't. Every answer is scored at
  both ends of ε's plausible range, not just the central estimate.

Alternative rules (bands of ±zσ√T, fixed-percentage widths, never moving) are
implemented as pluggable strategies. They are the baselines the optimiser has to
beat, not straw men.

## 4. Rebalancing with discipline

Most liquidity losses come from acting too often or for the wrong reasons. The
action queue makes every action argue for itself in dollars.

- **Candidates per position:** re-range to the optimal band, re-range and resize,
  close, or harvest and compound. Each is scored over the horizon against
  *staying exactly as it is*.
- **One pool at a time, in order.** Within a pool, the best ticket is applied as
  if done and the rest are re-scored, because two actions that each assume the
  other doesn't happen can both be wrong.
- **Costs stated, never assumed.** The swap needed to rebalance the token mix is
  measured. If no swap cost is configured, the ticket states the cost at which it
  breaks even instead of pretending the swap is free. The loss a close makes
  permanent is shown on every ticket.
- **Robust or not.** A ticket that stays positive across σ's whole confidence band,
  with every cost known, is marked *act now*. Anything else is *consider*.
- **Propose, never execute.** Tickets are instructions for a human signer on the
  multisig. The tool holds no keys. Tickets are marked done or snoozed for a set
  time; when a snooze lapses, the ticket comes back if it is still warranted.
- **Backtested against reality.** The optimiser is replayed over pool history
  against the book *as it actually stood*, comparing predicted gain with what
  happened. That replay is the check on whether the model deserves trust.

## 5. Watching risk

- **Shock grid:** the book revalued under token moves in both directions,
  mapped onto whichever side of each pair the token sits.
- **Greeks per position:** delta and gamma of a concentrated position, the
  probability of leaving its range over the horizon, and the fee yield it needs
  to break even against its expected loss.
- **Alerts that mean something:** out of range, near the edge, loss past a
  threshold, negative carry (expected fees below expected loss), stale data,
  rebalance due, policy breach. Each is deduplicated, and reminders are paced, so
  an alert is read rather than ignored.

## 6. Governance: a mandate you can audit

- **Limits are data.** Concentration (per position, pair, venue, chain), loss,
  out-of-range share, stablecoin depeg and counterparty exposure live in a policy
  file. A limit nobody has set reports as **NOT SET**, never as passing, and a
  malformed policy file raises an alert instead of loading as "no limits".
- **Breaches are episodes.** A breach opens an exception that stays open until a
  reading shows it has ended. Not being able to measure doesn't close it.
  Clearing is not the same as reviewing: someone signs off with a disposition
  and a note.
- **Incidents have a lifecycle:** detected, escalated, contained, resolved. A
  postmortem is required to resolve.
- **Monthly report, written once.** It attributes results against the HODL
  benchmark and lists exceptions and incidents. Once final, regenerating it
  needs an explicit override, because a reported month should not quietly change.

## 7. Holding market makers to their contracts

- **Contracts as a ledger.** Loans and option tranches are versioned with their
  source documents and marked at the current token price. The price is a market
  read, cross-checked against the price implied by the book's own pools, with the source
  and time shown.
- **Delivery verified, not self-reported.** Each market maker's depth and spread
  are tracked against what it committed to, and checked independently against
  each exchange's public order book.
- **Quotes compared on one scale.** Every RFQ response is priced for its own term
  and per unit of ±2% depth it commits, so bids of different shapes become
  comparable. Renewal and notice dates sit on a calendar so they can't pass unnoticed.

## 8. Data integrity is a risk control

- **Unknown is not zero.** A failed read is shown as unknown and excluded, with
  the value it leaves out. It is never booked as $0 or as a closed position.
  Ruling a live position closed because one RPC call failed is the kind of error
  this rule exists to prevent.
- **Pipeline health.** Every wallet, chain and protocol records when it last
  actually synced. A source that stops quietly is visible the same day.
- **Secrets out of every output.** API keys are stripped from every error before
  it is logged, printed or stored.

## 9. Next

- Custody reconciliation against the treasury's vaults.
- Execution packaging: tickets exported as ready-to-sign multisig transactions,
  still signed by a human.
- More venues on the shared concentrated-liquidity math.

---

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the system is built, and the
[README](../README.md) to run it.
