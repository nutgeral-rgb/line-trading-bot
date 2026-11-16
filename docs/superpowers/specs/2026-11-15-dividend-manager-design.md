# Dividend goal and portfolio review

## Confirmed intent
The user chose a fixed ten-year goal of THB 15,000 average NET monthly dividends, with variable actual payment months. Contributions are at least THB 1,000/month, possibly more. About THB 12,000 cash plus screenshot securities THB 28,816.80 are planning inputs, not a verified broker import. Risk tolerance is approximately 10% of this portfolio's capital. No promise of return or loss ceiling; no brokerage execution.

## Approaches
Risk-first with flexible deadline and fixed-deadline with explicit funding shortfall were offered. User selected fixed deadline. Implement transparent deterministic scenarios and portfolio review using existing storage and data providers; do not increase assumed returns or risk to make the goal appear achievable. A full allocation optimizer is deferred because reliable sector-specific inputs and verified historical ledger are absent.

## Deliverable
1. Keep existing portfolios and daily screening intact. Remove the obsolete THB1,000 deposit cap in commands, re-enrollment, and backup validation.
2. Add persistent per-user dividend goal via `ตั้งเป้าปันผล 15000 10`; deadline is stored, never rolled forward on reads. Repeating the identical setup preserves the original deadline. `เป้าปันผล` and `วิเคราะห์พอร์ต` queue asynchronous reports with fresh price checks.
3. Reports show current marked portfolio only when every positive holding has a valid completed-session quote; incomplete totals are explicitly partial, never replaced by cost. Show weights, concentration and hypothetical 15% securities stress, not an invented historical drawdown. Include holding review evidence for ordinary shares; warrants and REITs require instrument-specific verification and must not receive ordinary-company buy/sell conclusions.
4. Goal scenarios: net income yield 3/4/5%, effective annual total return 4/6/8%, end-month deposits, dividends reinvested during accumulation, nominal THB. Report required contribution, planned contribution, actual current-month deposits and remaining shortfall separately. No projected contribution creates cash. Expired deadlines have no fabricated required monthly number.
5. Add actual income journal commands `บันทึกปันผล SYMBOL GROSS TAX YYYY-MM-DD REF` and `บันทึกคืนทุน SYMBOL GROSS TAX YYYY-MM-DD REF`. Unique references make repeated submissions idempotent. Net cash is credited only once; capital returns are separate from dividends. Corrections use an explicit reversal command; do not infer historic tax or purchase rights from a screenshot.
6. Allow warrants with hyphenated symbols and explicitly zero-cost holdings; preserve instrument metadata across holdings updates and backups. Add explicit instrument status/type command. Do not guess a warrant's executable value from its last price while SP/delisted.
7. Add the portfolio goal summary to existing weekly closed-day reports for users who enabled a goal; do not change the chosen daily screening schedule.

## Scope limits
No live broker balances are overwritten. Existing simulated accuracy remains a separate report; its delivery-time/fill model needs a later dedicated correction and cannot validate this income strategy. Tax values are actual user entries, not tax advice. Goal is nominal, not inflation-adjusted. Free source delays remain visible. No autonomous learning or unvalidated strategy switching.

## Acceptance
Baseline and new unittest suite pass. Test the screenshot's 8 holdings including zero-basis warrant; deposits greater than1,000; backup round-trip; missing/stale/future/nonfinite quotes; fixed deadline and leap day; finite validation; expired goals; duplicate and reversed income; income user isolation; background dispatch without webhook market fetches; planned deposits never alter cash. Preserve old files and record recovery state.
