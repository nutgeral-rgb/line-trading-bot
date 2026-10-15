# Portfolio data storage

## Free PostgreSQL storage

The bot can store portfolios in PostgreSQL using the `DATABASE_URL` environment variable. If it is not set, the bot keeps using `portfolio.json`; that file is temporary on Render Free and can disappear after a restart, deploy, or spin-down.

A small free PostgreSQL service such as Neon Free can be used while its current free quotas are sufficient. Check the provider's current limits before use; free services can sleep or change their terms.

## Before switching

Check the current LINE values with `ดูพอร์ต` and `รายการติดตาม` and note anything you need to keep. Render Free may discard the local JSON file on any deploy or restart. The migration can import the file only if it is still present when the new code first connects to the database; it cannot recover data that Render has already discarded.

## Setup

1. Create a PostgreSQL project on a provider with a free plan.
2. Copy its PostgreSQL connection string and keep it private.
3. Merge this code change so the application knows how to use PostgreSQL.
4. In Render, open the `line-trading-bot` service, then **Environment**. Add `DATABASE_URL` as a secret value. Saving it may redeploy the service.
5. Send `ดูพอร์ต` in LINE to create or confirm your user portfolio, then `สถานะแผน` to check plan status.

The application creates its table on first use. When the database table is empty, it imports any portfolio data still present in `portfolio.json`. The bot does not log the database connection string. If PostgreSQL is configured but unavailable, portfolio operations fail visibly instead of silently falling back to an empty local file.

Neon's Free plan currently lists 1 GB storage, 100 CU-hours per project per month, 5 GB egress per project, and compute sleep after 5 minutes. This small portfolio workload should fit comfortably when used lightly, but the free quotas and provider terms can change. Monitor usage and verify the current limits in the provider dashboard.
