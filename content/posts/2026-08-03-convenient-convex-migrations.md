---
title: More convenient Convex migrations
description: "Making Convex migrations adaptive, resilient, and easier to target with multiple indexed ranges."
publishDate: 2026-08-03
tags: ["app development", "operations"]
pinned: false
draft: false
---

[Migrations](https://www.convex.dev/components/migrations) are a relatively frustrating part of the Convex development and operational experience, compared to most other standard backend frameworks for working with RDBMSs like Postgres or MySQL.


(1) **There is no way to make an atomic migration or "lock up" the database for the time of the migration**, but the migration itself is just a series of standard Convex mutations and actions, so the app itself also cannot simply be stopped for the time of the migration, short of temporarily removing *all* functions from your app, which itself also cannot be easily done if there are active crons referencing these functions.

(2) The only form of structural migration of a table is updating all data in the table or moving all data to a new table, serially. There are no atomic or cheap ways to remove columns from the table, rename columns in the table, or rename the table.

(3) [Convex's transaction limits](https://docs.convex.dev/production/state/limits#transactions) are such that migrations that do something non-trivial, that is, query multiple documents (perhaps in multiple tables) to determine how to update (migrate) a single document, can exhaust limits such as the 16 MiB data read limit at quite small batch sizes. **To make the migration succeed, the batch size must be such that it doesn't exceed the transaction limits in *any batch* when migrating the whole table.** Sometimes, due to data distribution skews, or disproportionately large rows in some document intervals, this means that the batch size should be quite small (e.g., 20 or 50 documents), which makes the overall migration of the table with hundreds of thousands of documents very slow. In my experience, migrations running for longer than 30 minutes on tables containing fewer than 500k documents were common. Luckily, [my application](/posts/about-blog-and-my-app/) has a relatively small amount of data. The migration experience in systems where tables contain more than a million documents could be even worse.

(4) The default batch size in migrations is 100 documents. This is actually quite small and causes migrations to take a long time even when the migration is actually simple (e.g., just deleting columns in a table); the batch size could be up to 5000 documents, and the migration could take an order of magnitude less time (e.g., 2 minutes instead of 20 minutes). Unfortunately, choosing the optimal batch size takes cognitive effort (who of us likes to apply cognitive effort?), and choosing the wrong size risks failing the whole migration (see the previous paragraph).

While (1) and (2) are the limitations of [convex-backend](https://github.com/get-convex/convex-backend) (i.e., the Convex platform itself), (3) and (4) are the shortcomings of the [standard migrations component](https://github.com/get-convex/migrations) that don't have any deep underlying reason to exist.

Hence I patched these shortcomings in the [get-convex/migrations#57](https://github.com/get-convex/migrations/pull/57) pull request.

Here's what the patched component contributes:

### Adaptive batch sizing: processing tables faster when migrations don't hit the transaction limits

When you do not pass an explicit batch size for a migration run, the patched component treats the migration definition's `batchSize`, `defaultBatchSize`, or `100` as the *starting* size and then **adjusts future batches from Convex transaction metrics. After a successful batch, it estimates the largest next batch that would keep the highest transaction-limit usage at or below half of the respective limit.** If adding one more document would be expected to cross that half-limit target, the batch size stays unchanged. If the last batch used more than half of a limit, the next batch shrinks toward the same target.

Concretely, the migration runs an action in which it processes each batch as a separate mutation. Let's say the initial mutation used the default batch size of `100` documents. When this mutation returns, the migration action checks the statistics of that batch mutation. It checks [all transaction limits](https://docs.convex.dev/production/state/limits#transactions), but the two most relevant ones are `bytesRead` and `documentsRead`. Their limits are 16 MiB and 32k documents, respectively. The third most relevant limit is that [a mutation's execution time shouldn't exceed 1 second](https://docs.convex.dev/production/state/limits#execution-time-and-scheduling).

Let's say that the initial batch mutation read 1 MiB of data, accessed 500 documents, and took 100 ms to complete. The patched migrations component will assess that:

 - the `bytesRead` limit could have permitted approximately 16 MiB / 1 MiB = 16 times more documents in the mutation.
 - the `documentsRead` limit could have permitted approximately 32000 / 500 = 64 times more documents in the mutation.
 - the mutation execution time could have permitted 1000 ms / 100 ms = 10 times more documents in the mutation.

The adaptive migrations logic takes the **least** of these potential increases (the most conservative one). In this case, this is the execution time limit, which permits increasing the batch size "only" by a factor of 10. Then, the migration logic will still **halve** that, just to be extra safe and conservative, so that no transaction limit exceeds 50% of the allowed maximum: 10 / 2 = 5. So, the next batch will be run with 100 * 5 = 500 documents instead of the 100 documents in the first batch.

After the next batch mutation with 500 documents completes, the logic above is repeated. If 50% of any of the transaction limits is exceeded (e.g., the batch mutation took 600 ms, which is more than half of the permitted limit of 1000 ms), the batch size will be adjusted down to stay within 50% of each limit. If the mutation is still below 50% of every limit, the next batch size could still be scaled up for the subsequent batch.

### Resiliency: reducing the batch size and continuing the migration automatically when bumping into transaction limits or OCC conflicts

The adaptive batch sizing could still cross some mutation limits if the data distribution is very uneven in different batches. When this happens, the patched component doesn't give up but still attempts to continue. The patched component detects whether the mutation failure is one of the "recoverable" types—a resource limit being exceeded, a mutation execution timeout (1 second), or an [OCC write conflict](/posts/occ-write-conflicts/) (which can happen if the migration processes the recent documents which are also updated by normal production mutations concurrently)—and then **halves the batch size and tries again**. It truly gives up only when it has reduced the batch size down to a single document but the mutation still fails for one of these reasons.

### Specifying multiple ranges for a migration

This is a smaller quality-of-life change. The standard migrations component only permits a single indexed range in `customRange`. Before the patch, applying the same migration to two disjoint indexed subsets required two migration definitions:

```ts
export const migratePendingJobs = migrations.define({
  table: "jobs",
  customRange: (query) =>
    query.withIndex("by_status_completedAt", (q) => q.eq("status", "pending")),
  migrateOne: migrateJob,
});

export const migrateRunningJobs = migrations.define({
  table: "jobs",
  customRange: (query) =>
    query.withIndex("by_status_completedAt", (q) => q.eq("status", "running")),
  migrateOne: migrateJob,
});
```

After the patch, `customRange` can return an ordered array of indexed ranges:

```ts
export const migrateActiveJobs = migrations.define({
  table: "jobs",
  customRange: (query) => [
    query.withIndex("by_status_completedAt", (q) => q.eq("status", "pending")),
    query.withIndex("by_status_completedAt", (q) => q.eq("status", "running")),
  ],
  migrateOne: migrateJob,
});
```

The component completes the ranges in array order. It does not make the ranges run in parallel. The practical benefit is narrower indexed scans. In fact, on particularly large tables, when there *is* a useful way to narrow down the migration subset with a simple enum-like field like `status` (or anything else), but there isn't an appropriate index for that in the table definition (the index should start with the discriminating field of interest), it could even make sense to *add such an index just for the purpose of running a single migration*. Convex will do "double work": first the index backfill (on the whole table!), and then separately the migration, but index backfill is implemented in Rust, not TypeScript, and runs substantially faster than migrations, so the effective *total running time* of such a migration process might still come out shorter than processing all rows (and skipping some) without a `customRange`.

In my application, agents use this feature in about 10% of the migrations.

## Using this patch component

The simplest way to use [get-convex/migrations#57](https://github.com/get-convex/migrations/pull/57) is to make a patch out of it and apply it via [patch-package](https://www.npmjs.com/package/patch-package) (ask your agent to do this).

The patched component is fully API-compatible with the standard component and doesn't require migrations to be used; however, it adds some fields to the component schema. So, if, after trying it, you decide to roll back to the standard component, you will need to write a migration that removes these fields from the component schema.

Disclaimer: I do not use *exactly* this patched component in my production Convex application. I use its equivalent that has exactly the same logic, but is not packaged as a component: rather, just a simple module in my application.
