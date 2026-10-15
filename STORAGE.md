# Portfolio data storage

## Keep data without a paid Render disk

The bot can store portfolios in PostgreSQL using the `DATABASE_URL` environment variable. If it is not set, the bot keeps using `portfolio.json`; that file is temporary on Render Free and can disappear after a restart, deploy, or spin-down.

A small free PostgreSQL service such as Neon Free can be used while its current free quotas are sufficient. Check the provider's current limits before use; free services can sleep or change their terms.

1. Create a PostgreSQL project on a provider with a free plan.
2. Copy its PostgreSQL connection string. Keep it private.
3. In Render, open the `line-trading-bot` service, then **Environment**. Add `DATABASE_URL` with the connection string as a secret value.
4. Deploy the branch containing this change. The application creates its table automatically.
5. Send `ดูพอร์ต` in LINE to create or confirm your user portfolio, then `สถานะแผน` to check plan status.

When the database table is empty, the bot imports any portfolio data still present in `portfolio.json` on its first database read. If Render has already discarded the old local file, that data cannot be migrated from the new deployment; check `ดูพอร์ต` and `รายการติดตาม` before switching storage and record any data that must be restored.

The bot does not log the database connection string. If PostgreSQL is configured but unavailable, portfolio operations fail visibly instead of silently falling back to an empty local file.

Neon's Free plan currently lists 1 GB storage, 100 CU-hours per project per month, 5 GB egress per project, and compute sleep after 5 minutes. Usage beyond a free allowance may require a plan change or service interruption; verify current terms in the provider dashboard.
