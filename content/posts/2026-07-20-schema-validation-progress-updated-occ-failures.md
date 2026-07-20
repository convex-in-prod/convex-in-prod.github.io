---
title: schema_validation_progress_updated OCC Failures
description: "How Convex schema validation progress can cause production mutations to exhaust their OCC retries."
publishDate: 2026-07-20
tags: ["internals", "self hosted", "operations"]
pinned: false
draft: false
---

After writing the post about [OCC write conflicts](/posts/occ-write-conflicts/) in Convex, I encountered another type of OCC failure in my application that I didn't know about before. These OCC write conflicts have `occ_write_source: "schema_validation_progress_updated"` in their diagnostic metadata, which means that my application's mutations conflict with Convex's internal process of _schema validation_ during Convex deploys.

When you deploy your Convex app to a Convex Cloud deployment or a self-hosted Convex server, Convex [checks that existing documents match a new or changed schema](https://docs.convex.dev/database/schemas#schema-validation) before activating it. If an incompatibility is found, the deploy is aborted with a message such as `Document with ID "..." in table "..." does not match the schema: ...`, which is probably familiar to every Convex developer.

For a `schema_validation_progress_updated` OCC failure to happen, a document written by a concurrent mutation must mismatch the schema being deployed. The immediate trigger is therefore an attempt to deploy a schema that doesn't match data that is being written in production. However, this doesn't excuse the OCC failure _of a production mutation_, considering that it is a valid and, in fact, efficient workflow in Convex to attempt deploying a schema of unknown validity to determine whether writing any [migration](https://github.com/get-convex/migrations) is needed.

### Mechanics of `schema_validation_progress_updated` OCC failures

At the beginning of schema validation for the root app or a component, Convex creates two internal documents: a document in the `_schemas` system table with the state of the new schema (`Pending`, and eventually `Failed`, `Validated`, or `Active`) and a document in `_schema_validation_progress` with the schema ID, the approximate total number of documents to check, and the number checked so far. The progress document is for the whole schema, which may require walking several tables; it doesn't contain a cursor for one particular table.

A background task inside `convex-backend`, called [`SchemaWorker`](https://github.com/get-convex/convex-backend/blob/415bab2c51e7d03decef10f43f8a60a928d1e20e/crates/application/src/schema_worker/mod.rs#L135-L272), walks the tables that require validation and periodically writes a checkpoint to the `_schema_validation_progress` document. The checkpoint threshold is about 5% of the documents to check, capped at 500 ([`ceil(total / 20).min(500)`](https://github.com/get-convex/convex-backend/blob/415bab2c51e7d03decef10f43f8a60a928d1e20e/crates/application/src/schema_worker/mod.rs#L388-L417)).

If there is a mutation that patches or inserts a document in a table while schema validation is in progress, the mutation validates its proposed document against both the active schema and the new schema. If the document fails the active schema, the mutation itself fails normally. But if it matches the active schema and doesn't match the new schema, **the mutation is allowed to proceed and marks the new schema as `Failed` and deletes the corresponding `_schema_validation_progress` document inside the same transaction.** The latter adds that `_schema_validation_progress` document to our mutation's [read set](https://stack.convex.dev/how-convex-works#read-and-write-sets).

The OCC failure, that is, the exhaustion of all five attempts to execute the mutation (see the [previous post](/posts/occ-write-conflicts/) for details), happens when several unfortunate conditions coincide:

1. As I noted above, a document being written concurrently must be consistent with the active schema and inconsistent with the new schema.
2. At the same time, the background validation should continue making progress rather than finding an incompatible existing document and failing the schema immediately. This means that all the documents encountered earlier in the scan are compatible, or that the incompatibility exists only in data being written concurrently by mutations.
3. Checking one checkpoint batch must be significantly faster than the mutation, so whenever the mutation is retried, at least one `_schema_validation_progress` update commits while the mutation is in progress and invalidates its read set.

In the actual production incident that I had, progress checkpoints committed about once every 65 ms, while the affected mutation attempts took about 150 ms.

There are multiple factors that can contribute to the relative speeds of schema validation checkpoints and mutations, such as the complexity of the schemas of the particular tables, whether the data being validated is cached (either on the Convex or underlying storage side), document sizes, and, of course, the complexity of the mutation.

The `schema_validation_progress_updated` OCC failure in my application happened on a self-hosted Convex server that colocates `convex-backend` (the Convex runtime) and MySQL (which I use as the data storage) on the same server. I don't know whether the proximity between `convex-backend` and MySQL contributed to the relative speed of schema validation. Another contributing factor might have been that the server has a lot of spare memory and MySQL can hold essentially the entire database in its buffer pool.

I also checked whether my application had these OCC write conflicts back when it was hosted in Convex Cloud. I found one `schema_validation_progress_updated` conflict, but the mutation succeeded on its next attempt. During another deploy, there were two conflicts with `schema_validation_tracker_initialized`, and those mutations also succeeded after retrying. So Convex Cloud in principle could have this type of OCC write conflicts as well as self-hosted Convex. I don't know whether the fact that on those occasions there weren't actual OCC failures of mutations (with the exhaustion of all OCC retires) is due to relatively lucker circumstances of those deployments and the tables and mutations involved, or because checkpoints don't commit as fast in Convex Cloud (although I don't have particular reasons to think so because I didn't notice any significant difference in the speed of deployments beteween Convex Cloud and my self-hosted Convex setup).

### Preventing `schema_validation_progress_updated` OCC failures

It seems that the most principled fix is to **stop deleting the current `_schema_validation_progress` document inside the application mutation that fails the new schema**. The mutation still writes the schema state to `Failed`, but `_schemas` is not updated repeatedly like `_schema_validation_progress`. To keep the behavior correct, every progress initialization and checkpoint transaction starts to read the exact `_schemas` document and writes progress only while that schema is still `Pending`.

If a checkpoint commits first, the application mutation can now commit immediately afterward because it no longer reads the progress document. If the application mutation marks the schema `Failed` first, the checkpoint's `_schemas` read becomes stale and the checkpoint loses OCC; a later pass deletes the inactive progress document. This keeps the ordinary OCC ordering rules instead of adding a special exemption for system-table writes or another retry loop.

See the [patch to `convex-backend` that implements this fix](https://github.com/convex-in-prod/convex-backend/tree/main/patches/schema_validation_progress_occ) (the full patch is the commit in which the linked patch description is added), its `SchemaModel::mark_failed` change, and the schema-state fence on progress updates in `schema_validation_progress/mod.rs`.
