# Dividend manager implementation plan

**Goal:** Add the fixed-deadline income goal and auditable portfolio review agreed with the user.
**Architecture:** Pure calculations/journal in `dividend_goal.py`, valuation/report helpers in `portfolio_manager.py`, existing bot command routing and async worker integration. Existing PostgreSQL JSON remains additive.
**Spec:** ../specs/2026-11-15-dividend-manager-design.md
**Global constraints:** No automatic brokerage orders, no production portfolio replacement, no new paid service, no guaranteed yield/loss threshold. Keep original dirty checkout untouched.

## Tasks
- [x] 1. Baseline verification and recovery record. Compare preserved local diffs against immutable remote main0ff7d10; run complete baseline suite in isolated environment.
- [x] 2. RED/GREEN pure goal calculation and income ledger. `create_goal`, `project_goal`, `record_distribution`, `reverse_distribution`, and `income_summary` in dividend_goal.py. Test fixed date, zero/negative/finite inputs, 40,816.8 starting assets +1,000 end-month deposits →235,570.11 at6% over120months, net4% target4.5m and required27,246.94/month, no-fill missing capital, duplicate refs and reversal.
- [x] 3. RED/GREEN valuation/report and LINE integration. `value_portfolio` and `format_manager_report` in portfolio_manager.py; commands after storage check; background worker fetches completed-session data. Test stale/partial totals, zero cost and SP warrants, no false ordinary-company conclusions for REITs, no webhook data fetching. Remove contribution cap at all old paths; validate backup round-trip and no holdings loss.
- [ ] 4. Document commands, run all tests, review full diff, publish only verified changes on a new branch/PR; verify latest remote head before integration. Report deployed status accurately.

## Review focus
Stored goals and investment strategy must survive re-enrollment and backups; settings do not change cash. Cashflow corrections must not leave partial mutation. Missing instrument quotes do not fabricate NAV. Date deadlines remain anchored on repeated commands. All money validators reject NaN/infinity/negative and booleans from JSON.

## Recovery ledger
- Initial audit completed before interruption; no product code for dividend goal existed.
- Existing local checkout preserved with4 modified files on05d2360. New isolated snapshot fetched from GitHub0ff7d10; local baseline77aab5e.
- User explicitly requested continued implementation, selected fixed10year goal, and previously waived repeat edit/deploy confirmation. No further implementation approval requested.
- Initial test attempt failed due missing local dependencies, not test assertions. Creating isolated venv to run actual suite.
- Baseline17/17 passed after installing isolated dependencies and setting test TEMP to an allowed local directory. Old bot.py/long_term_review.py parse successfully; old ACCUMULATION.md and wake workflow match remote aside from line endings. Old source remains untouched.
- Task2 RED missing module →6/6 GREEN; Task3 RED missing module/old command routing →33/33 GREEN suite.
- Independent read-only reviewer found malformed restored distribution history and premature deadline message. Both reproduced RED; net arithmetic, references, reversal schemas and exact date now checked; final35/35 GREEN.
- Ruling: treat premature expiry as user-visible incorrect goal status and fix with the important backup validation in the same review fix pass.
- Ruling: preserve incomplete NAV as incomplete and withhold NAV-dependent monthly-funding estimates until quotes are complete. Cost-as-price would produce misleading advice; consequence is a report requiring data reconciliation for SP/stale positions.
- Reviewer declined financial forecasting/suitability, external source reliability, live LINE/PostgreSQL integration and the pre-existing delivery-time simulator. These remain explicit limits, not claims of verified behavior. No forecast guarantee or automatic orders introduced.
