# Portfolio data storage

## Durable PostgreSQL storage

The bot stores portfolios in PostgreSQL when `DATABASE_URL` is set. On Render it now refuses portfolio writes without a working database instead of pretending that a temporary JSON file is durable. A local development run can still use `portfolio.json`.

A small free PostgreSQL service such as Neon Free can be used while its current free quotas are sufficient. Check the provider's current limits before use; free services can sleep or change their terms.

## Before switching

**Before changing Render's environment or merging/deploying this change**, send `สำรองพอร์ต` to the currently running LINE bot. Save the returned JSON privately as `portfolio-backup.json`. This one-time migration backup is needed because changing an environment variable also restarts the free web service and may erase its old local file. The backup covers cash, shares, average cost, watchlist, and investment strategy; it does not include old history or old recommendation records. Check `ดูพอร์ต` against your brokerage account before migrating.

## Setup

1. Create a PostgreSQL project on a provider with a free plan.
2. Copy its PostgreSQL connection string and keep it private.
3. In Render, open the `line-trading-bot` service, then **Environment**. Add `DATABASE_URL` as a secret value and let the **current main version** deploy. Do not paste it in LINE, a GitHub issue, or this chat.
4. In LINE, send `ดูพอร์ต`. If it does not show the previous balance, send the saved `portfolio-backup.json` file to the bot once, then check `ดูพอร์ต` and `รายการติดตาม` again. Confirm the balance and holdings match your brokerage records **before merging the new code**.
5. Merge/deploy this code change. In LINE, send `สถานะข้อมูล`; it must report **PostgreSQL** with a working connection. Send `ตั้งแผนลงทุนระยะยาว` if needed. Set the actual balance with `ตั้งเงินสด 6000` only if it matches the brokerage account. Thereafter record real top-ups with `บันทึกเงินเติม 500`, and actual trades with `บันทึกซื้อ` / `บันทึกขาย`.

The application creates its tables on first use. If the database is empty and the old local file happens to survive the deploy, it imports that file; otherwise restore the one-time backup. Once PostgreSQL is active, later code deploys do not reset portfolio rows. Each portfolio edit saves a prior revision in PostgreSQL, keeping the latest 30 revisions per user. If PostgreSQL is unavailable, portfolio operations fail visibly rather than falling back to an empty local file.

Neon's Free plan currently lists 1 GB storage, 100 CU-hours per project per month, 5 GB egress per project, and compute sleep after 5 minutes. This small portfolio workload should fit comfortably when used lightly, but the free quotas and provider terms can change. Monitor usage and verify the current limits in the provider dashboard.
