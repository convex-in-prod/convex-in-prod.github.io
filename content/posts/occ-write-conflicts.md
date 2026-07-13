---
title: Handling OCC write conflict failures in Convex
description: "How to diagnose, mitigate, and prevent permanent Convex mutation failures caused by optimistic concurrency control write conflicts."
publishDate: 2026-07-13
tags: ["internals", "app development", "operations"]
pinned: false
draft: false
---

This post assumes that you are familiar with Convex's Optimistic Concurrency Control (OCC) model and have read the [How Convex Works](https://stack.convex.dev/how-convex-works) post, [Convex docs on OCC and Atomicity](https://docs.convex.dev/database/advanced/occ), and Jamie Turner's post [Why Convex Limits Transactions and How Concurrency Control Shapes Your Database](https://stack.convex.dev/convex-database-limits-explained).

In the post [about my Convex application](/posts/about-blog-and-my-app/), I credit Convex's OCC model for enabling the development of the monolithic "reactive core" of my application (consisting of webhooks, crons, workflows, user interactions, etc.) with an acceptable level of engineering complexity and effort. The application scaled to about 350 tables and hundreds of thousands of lines of production code in about nine months of mostly solo agentic coding. I don't think it would have been possible to develop the application as quickly against a database using pessimistic concurrency control (PCC), such as Postgres. Thus, I concur with the main idea of [Jamie Turner's post](https://stack.convex.dev/convex-database-limits-explained) that OCC scales better than PCC.

The flip side of Convex's OCC model is the risk of **permanent mutation failures due to OCC write conflicts**. I'll call them *OCC failures* for short below. By default (more on this below), this happens after the Convex backend attempts a mutation **five times** (the original attempt and four retries), encounters a conflict with another mutation each time, and loses every conflict. Scheduled mutations use a separate retry loop that continues beyond this limit.

**OCC failures are bad because the application could fail in unpredictable ways**: it could fail to persist user data, leave a workflow spanning multiple mutations in an inconsistent state, or become "stuck" in an important way, such as when the failed mutation was supposed to schedule a follow-up task. This is not unique to OCC failures in particular: other types of mutation failures, such as exceeding the mutation limits (one-second runtime, limits on documents or bytes read, etc.), are equally bad. However, they are far less common and usually reflect real programming bugs, whereas failures due to OCC write conflicts can appear in perfectly semantically correct Convex mutations simply because they aren't written "scalably enough."

To check whether your application has had OCC failures in the last three days, go to the Health page in your Convex Dashboard and scroll to Insights. OCC failures are marked "Critical":

![Critical Convex dashboard alerts showing mutations that failed due to write conflicts](/images/occ-write-conflicts-alerts.png)

Or, simply ask your agent whether there were permanent mutation failures due to OCC write conflicts recently. The agent can fetch them through [Convex MCP](https://docs.convex.dev/ai/convex-mcp-server).

As an application scales, both in the amount of code and number of tables and in production load, new instances of OCC failures crop up because it's hard to foresee all of them. Still, it's a good idea to **add questions to the code review checklist (such as for your agentic review loop) to catch code and schema designs that would almost definitely lead to OCC failures in production**, e.g.:

> - Would this change set almost definitely lead to significant OCC write conflict pressure that may lead to permanent mutation failures if deployed in production, given our typical load pattern and reasonable expectations about the frequencies of certain mutations and events? Sample Convex logs in Axiom if needed to assess the recent load and throughput of mutations. Don't report or fix overly hypothetical and unsubstantiated OCC write conflict possibilities; report or fix only those you are relatively confident about. For strategies to prevent OCC write conflicts in the Convex schema and code, see [this blog post](/posts/occ-write-conflicts/).

Some OCC failures will inevitably slip through in production. Monitor them regularly by checking the insights in Convex MCP and/or the Health page in Convex Dashboard.

In the course of developing and scaling my Convex application, I've observed and fixed dozens of distinct scalability defects in my codebase that caused OCC failures in production. Below, I'm sharing what I learned about OCC failures from that experience and how to manage them most effectively.

### OCC failures are surprisingly common in fairly mundane Convex code

[Convex docs on OCC failures](https://docs.convex.dev/error#1) sound a bit like these failures usually stem either from direct programming mistakes ("make sure you are not calling a mutation an unexpected number of times") or from "obvious" hotspot writes, such as updating a single document with a counter field from many concurrent mutations. In my experience, this is not the case.

I've had a few OCC failures that were the result of direct programming mistakes, such as doing fan-out mutations via `Promise.all(objects.map((obj) => ctx.runMutation(...)))` within an action and not noticing that these mutations update the same object (the obvious fix here is making these mutations sequential). Hot counter cases would cause OCC failures if implemented naively, but having read the Convex docs, I usually avoided committing such mistakes and used striping or delayed batch aggregation/computation.

But apart from these two categories of possible OCC failures, I actually had dozens of different failure patterns, with a surprising amount of variety among them (rather than a few shared themes like a hot counter), often in very mundane-looking Convex code.

For example, imagine there are a few documents with some business logic state, such as `TicketState`, `AccountBalance`, a chat with a user (a Convex table like `chatMessages`), and the LLM agent state that deals with this chat, which we'll call `LlmState`. There is some mutation that needs to commit a state transition, an automated response to the user, or anything else based on the up-to-date versions of these objects. `TicketState` and `AccountBalance` are updated about once per minute, with an `updatedAt` field written to them. If, by coincidence, the updates to `TicketState` and `AccountBalance` happen within one or two seconds of each other, *and* a message or two arrives in the chat from the user (adding a document or two to the `chatMessages` table), *and* the LLM state is updated around the same time by an LLM that has just finished processing the previous user message sent a minute ago, this is enough to make the unlucky mutation lose five times and fail permanently. This is especially likely if some of these updates to `AccountBalance`, `TicketState`, `LlmState`, etc. also consult one another's state during their respective mutations, causing a bit of an OCC write conflict retry storm; in this case, even less coincidence is required to make one of these mutations fail permanently.

Note that individually, none of these documents and tables are updated particularly frequently: this is not a real-time group chat with dozens of messages flying by per second. It's just that *sometimes* (even if rarely), three to five updates happen within a one- or two-second window. The onset of retry chains, with Convex OCC write conflict retries happening at randomized intervals within 0–100 ms, 0–200 ms, 0–400 ms, and 0–800 ms for retries #1, #2, #3, and #4 respectively, effectively stretches the "coincidence window" up to three or four seconds for the eventual losing mutation.

Another example: imagine there is some mutation that writes some state and needs to cancel some crons with a modest fan-out, e.g., about a dozen. Imagine a user belongs to some groups, and when the user account transitions to a particular state in the mutation, some periodic check-ups of the user in those groups, implemented as crons or periodic [scheduled functions](https://docs.convex.dev/scheduling/scheduled-functions), should be cancelled. It's very easy to write at the end of this mutation:

```ts
const accountGroupCheckUpsScheduledFnIds = await ctx.db.query(/* ... */);
await Promise.all(
  accountGroupCheckUpsScheduledFnIds.map((fnId) => ctx.scheduler.cancel(fnId))
);
```

But if five of these scheduled functions *start* within a few seconds of the mutation that transitions the account state, they update their own records as they run and complete, thus causing OCC write conflicts with the mutation that is attempting to update the statuses of these scheduled functions to `canceled`. This can make the mutation fail permanently. (Note that this example leads to an OCC failure even if the mutation doesn't cancel these scheduled functions but merely reads their statuses, because reading a document from within a mutation while that document is updated by another mutation also leads to an OCC write conflict in Convex.)

The immediate remedies for the OCC failures in these two examples are fairly trivial: in the first example, the fields that make each `TicketState` and `AccountBalance` sync tick update the documents, such as `lastUpdatedAt`, should either be moved into separate tables like `ticketStateUpdates` and `accountBalanceUpdates`, or removed altogether. In the second example, a separate mutation like `cancelCheckUp` should be added and these mutations should be *scheduled* (rather than called directly) from the parent mutation:

```ts
await Promise.all(
  accountGroupCheckUpsScheduledFnIds.map(
    (fnId) => ctx.scheduler.runAfter(0, mymodule.cancelCheckUp, { fnId })
  )
);
```

Another option for one-off scheduled functions is to stop explicitly cancelling them, let them start normally, self-validate (read the account state, in this example), and exit early. In fact, this would be nice to add for correctness anyway if cancellation is moved from within the mutation above into separate mutations scheduled via `ctx.scheduler.runAfter(0, ...)`, as in the snippet above, because the user state transition and cancellations are no longer within a single atomic mutation.

The point is not that these OCC failures are difficult to patch, but that they are rather easy to introduce into production code. This is why it's necessary to regularly monitor for OCC failures in Convex MCP/health insights.

### Fixing Convex's default retry policy on OCC write conflicts

An indirect reason why it's surprisingly easy to write a Convex application with OCC failures in production is that **the default number of attempts before a mutation fails permanently—five (the original attempt plus four retries)—is just too small.**

I've made a [patch for `convex-js`](https://github.com/get-convex/convex-js/pull/170), the dependency that a TypeScript codebase interacts with when writing Convex app code, browser code (such as React), and HTTP client code in backend workers (if written in JS/TS). It makes `ctx.runMutation` calls from actions within Convex, React client `mutation` calls, and HTTP client `mutation` calls retry once (configurable) after a two-second pause (also configurable) if the first call fails permanently due to OCC write conflicts. Since the five attempts discussed above happen internally, within the Convex runtime, the effective number of attempts that the Convex runtime will make before finally failing the mutation is ten.

Essentially, this patch is no different from a client-side wrapper around all `ctx.runMutation` calls in your code, but such wrappers add unnecessary boilerplate and make the Convex code look "nonstandard" to agents. Hence, it's better to operationalise this wrapping as a [`patch-package`](https://www.npmjs.com/package/patch-package) patch for the `convex` npm dependency. Just ask your agent to make a `patch-package` patch out of [convex-js#170](https://github.com/get-convex/convex-js/pull/170).

After applying this patch and running it in production for about a month, I've seen very few OCC failures that would have been permanent without this patch and remained permanent after applying it (that is, effectively failing ten attempts in a six- or seven-second period). In fact, the "remaining" failures could be classified as programming mistakes, unlike the OCC failures in seemingly benign Convex code discussed above, which appear frequently when only five attempts are made before permanently failing a mutation. In [convex-js#170](https://github.com/get-convex/convex-js/pull/170), I wrote that 13 or 14 out of 15 OCC failure patterns were effectively "salvaged" by the patch; since then, I have seen perhaps 20–30 more OCC write conflict patterns in my application, and this proportion has held up.

The recording of OCC failures for Convex MCP/health insights happens on the Convex runtime side. Since the Convex runtime (either cloud or self-hosted) knows nothing about my patch, I still see "Critical" OCC write conflict errors in the insights (after five attempts), even though they are later retried from the client side after a two-second delay and eventually succeed. This is actually convenient because it allows me to continue focusing on these "pseudo" OCC failures via Convex MCP insights and fix them before they have a chance to worsen and start failing even with the `convex-js` patch.

I conclude that a total of only five attempts before permanently failing a mutation is a bad default because it leads to permanent mutation failures in certain Convex code, schema, and mutation patterns that are otherwise benign and correct, when only a very infrequent, unlucky congregation of four or five mutations within a one- or two-second window makes one of these mutations fail. Even though this is easily fixable (as in the example above, and I will enumerate all of the approaches below), it makes a Convex app less reliable and pushes for more table splits in the schema design, finer-grained mutation splits, and conversions of some previously atomic mutations into query and mutation choreography within an action. The latter is much harder to program correctly while avoiding race conditions.

Convex developers didn't answer my question in [convex-js#170](https://github.com/get-convex/convex-js/pull/170) about the motivation behind the default of five attempts. [Jamie Turner's post](https://stack.convex.dev/convex-database-limits-explained) explains the mutation's one-second runtime and 1 MB read limits, but doesn't explain the retry limit either.

I'd guess that the reasoning was approximately that permitting up to ten attempts could, together with inefficient Convex programming and/or some unfortunate load pattern, lead to situations where some mutations consistently retry three to six times and take two to three seconds to complete. This would lead to both poor user experience and amplification of cloud database costs (because each mutation retry adds to the total database I/O and the number of Convex function calls). After deploying my patch, I even encountered something like this situation once: a hot mutation had a lot of OCC write conflict retries. But the total number of mutation calls that were retries still didn't exceed 25% of the total "useful"/logical mutation calls.

I'd counter such reasoning by saying that OCC failures also lead to a bad user experience, sometimes not recoverable without admin intervention, e.g., to unstick some workflows whose driving scheduled functions were not scheduled because a mutation failed permanently, or to fix inconsistent database state. Overall, as a Convex application operator, I strongly prefer fewer OCC failures (hence fewer user-visible application failures) at the cost of the risk of occasional OCC write conflict thrashing and slightly elevated database costs. Both of the latter situations are easily discoverable through Convex MCP/health insights, and they can usually be fixed just as straightforwardly as OCC failures.

### Diagnosing OCC failures

Convex MCP/health insights return the following structure:

```js
{
  insights: [{
    kind: "occFailedPermanently" | "occRetried",
    // The "error" severity is called "Critical" in Convex Dashboard
    severity: "error" | "warning",
    functionId: string,
    componentPath: string | null,
    occCalls: number, // Aggregate over 72h
    // For errors (OCC failures), this is the table name for the conflict
    // document for the last retry attempt. Each OCC write conflict is
    // attributed to a specific document which both the winning and the losing
    // mutations accessed.
    occTableName?: string,
    // At most 5 events. Don't confuse with 5 attempts within a single "error" event.
    // Individual OCC write conflict retries are not represented in Convex MCP results.
    recentEvents: [{
      timestamp: string,
      id: string,
      request_id: string,
      occ_document_id?: string, // ID of the conflict document
      // The winning mutation or other source, such as `scheduled_job_mutation_success`
      occ_write_source?: string,
      // 4 for "error" events, the original attempt is not counted.
      occ_retry_count: number,
    }],
  }],
  summary: string,
  dashboardUrl: string,
}
```

To better understand an OCC failure—the full retry chain and timing, and the full list of winning mutations—the insights from Convex MCP are insufficient. You need to explore the Convex logs from a few seconds before the OCC failure's `timestamp` to find the logs of the first four attempts that led up to the permanent mutation failure, and the logs of the concurrent mutations that likely conflicted with the failed mutation and won. Because these Convex logs are likely a few hours or days in the past, they should be queried through Axiom or another log store that you use for Convex logs.

Convex MCP/health insights are available only in Convex Cloud, not in self-hosted Convex. Fortunately, all the information from Convex MCP insights about OCC failures is also recoverable from Convex logs alone, only without a nice, succinct interface. I use the following section in my project's SRE agent skill:

<details class="my-6">
<summary class="text-accent-2 cursor-pointer font-semibold">Show the SRE agent skill excerpt</summary>

````markdown
## Convex OCC Failures

Self-hosted Convex `function_execution` logs retain OCC metadata in Axiom. Read the detailed queries
and field semantics in [Axiom](../axiom/SKILL.md) under `Investigate Convex OCC conflicts`.

Use these query shapes directly for the standard 72-hour check and reconstruction:

```bash
# Backend invocations that exhausted Convex's internal OCC retries.
axiom query "['prod'] | where ['convex.deployment_name'] == 'convex-self-hosted' and ['data.topic'] == 'function_execution' and ['data.status'] == 'failure' and ['data.will_retry'] == false and isnotnull(['data.occ_info.table_name']) | extend functionPath=tostring(['data.function.path']), requestId=tostring(['data.function.request_id']), tableName=tostring(['data.occ_info.table_name']), sample=pack('requestId', ['data.function.request_id'], 'documentId', ['data.occ_info.document_id'], 'writeSource', ['data.occ_info.write_source'], 'retryCount', ['data.occ_info.retry_count']) | order by _time desc | summarize exhaustedInvocations=count(), uniqueRequests=dcount(requestId), samples=make_list(sample, 10) by functionPath, tableName | order by exhaustedInvocations desc" --start-time "-72h" -f json

# Retried backend attempts, including their competing writer source.
axiom query "['prod'] | where ['convex.deployment_name'] == 'convex-self-hosted' and ['data.topic'] == 'function_execution' and ['data.will_retry'] == true and isnotnull(['data.occ_info.table_name']) | summarize conflictAttempts=count(), affectedRequests=dcount(['data.function.request_id']), maxRetryCount=max(['data.occ_info.retry_count']) by functionPath=['data.function.path'], tableName=['data.occ_info.table_name'], competingWriteSource=['data.occ_info.write_source'] | order by conflictAttempts desc | take 50" --start-time "-72h" -f json

# One mutation's backend retry attempts. Keep both request ID and function path filters.
axiom query "['prod'] | where ['convex.deployment_name'] == 'convex-self-hosted' and ['data.topic'] == 'function_execution' and ['data.function.request_id'] == '<requestId>' and ['data.function.path'] == '<functionPath>' | project _time, ['data.execution_time_ms'], ['data.user_execution_time_ms'], ['data.status'], ['data.will_retry'], ['data.function.mutation_retry_count'], ['data.occ_info.retry_count'], ['data.occ_info.table_name'], ['data.occ_info.document_id'], ['data.occ_info.write_source'] | order by _time asc" --start-time "<narrow-absolute-start>" --end-time "<narrow-absolute-end>" -f json

# Parent and dependency calls sharing that request ID.
axiom query "['prod'] | where ['convex.deployment_name'] == 'convex-self-hosted' and ['data.topic'] == 'function_execution' and ['data.function.request_id'] == '<requestId>' | project _time, ['data.function.path'], ['data.function.type'], ['data.execution_time_ms'], ['data.status'], ['data.will_retry'], ['data.function.mutation_retry_count'], ['data.occ_info.table_name'], ['data.occ_info.document_id'], ['data.occ_info.write_source'] | order by _time asc" --start-time "<narrow-absolute-start>" --end-time "<narrow-absolute-end>" -f json

# Candidate competing executions. Convert a source such as module.js:function to module:function.
axiom query "['prod'] | where ['convex.deployment_name'] == 'convex-self-hosted' and ['data.topic'] == 'function_execution' and ['data.function.path'] == '<normalizedCompetingFunctionPath>' | project _time, ['data.function.request_id'], ['data.execution_time_ms'], ['data.status'], ['data.will_retry'], ['data.function.mutation_retry_count'] | order by _time asc" --start-time "<narrow-absolute-start>" --end-time "<narrow-absolute-end>" -f json
```

Apply these rules:

1. Filter Axiom to `convex.deployment_name == "convex-self-hosted"`.
2. Require `status == "failure"`, `will_retry == false`, and a present `occ_info.table_name` to
   identify one backend invocation that exhausted its internal OCC retries. Do not yet call that a
   failure that was visible to the caller.
3. Group first by losing function path and contested table. Compare conflict record count with
   distinct losing request IDs.
4. Use `write_source` to identify the competing writer and `document_id` to determine whether one
   hot document dominates. `write_source` is not the competing request ID.
5. Correlate the losing request ID and event `_time` with nearby function, worker, and application
   logs. `_time` is not the internal timestamp of the competing write.
6. Treat `will_retry == true` rows as contention evidence, not permanent failures. Analyze them when
   they are frequent, retries are deep, or they explain permanent failures.

For every invocation that exhausted its backend retries, and for any significant cluster of retried conflicts, proactively
reconstruct the losing mutation's retry chain and the nearby mutation calls. Do not stop at a grouped
conflict count. Use the same `data.function.request_id` and `data.function.path` to collect all
attempts of the losing mutation in time order. A request ID can also cover a parent action and other
function calls, so request ID alone does not define one mutation retry chain.

Account for two retry levels:

- Convex backend retries one invocation internally. These attempts share request ID and function
  path, and `data.function.mutation_retry_count` increases within the invocation.
- [`scripts/patch-convex-occ-retry.mjs`](../../../scripts/patch-convex-occ-retry.mjs) patches the
  installed `convex` package's HTTP client, WebSocket client, and action `runMutation` implementation. When
  one backend invocation exhausts OCC retries, these callers wait `2000 ms` and repeat the complete
  mutation once with a fresh backend retry budget. Direct backend execution paths that do not call
  through the patched `convex` package do not receive this outer retry.

The outer retry can keep the same request ID for an action's `runMutation`, or use another request ID
for browser/HTTP client calls. Identify its boundary by the row that exhausted the backend retries followed about two
seconds later by the same mutation with `mutation_retry_count == 0`. Use caller/application logs to
link different request IDs. When many calls to the same function run concurrently, timing and function path alone provide
only a probable correlation. A successful outer invocation means the earlier invocation was
recovered and did not result in a permanent failure visible to the caller. Classify an effective permanent failure
only after checking the outer retry and caller outcome.

Present the reconstruction with these columns:

| Call / try | Approx. started at | Runtime (ms) | Result | Conflict document / table | Competing `write_source` | Competing execution / start |
| --- | --- | --- | --- | --- | --- | --- |

Put the outer call number, backend try number, and request ID in `Call / try`. Use
`data.function.mutation_retry_count` for the backend try number. Derive approximate start time by
subtracting `data.execution_time_ms` from `_time`; label it approximate. Merge function execution and
commit/retry state in `Result`: for example, `committed`, `OCC; backend retry`, or `backend retries
exhausted`. A row with `status == "success"` and `will_retry == true` means the function returned but
its commit conflicted. Include the later `will_retry == false` row that committed or exhausted the
invocation, plus the outer invocation when the patched client made one.

Then search the same narrow time window for executions whose function path matches the competing
`write_source`, plus related calls sharing the losing request ID. Record plausible competing
executions and their approximate starts. The OCC event does not contain the competing request ID or
the internal timestamp of the conflicting write. A match by path and time is therefore a candidate, not proven
winner attribution; write `unknown` when no independent request/log correlation establishes it.

Keep document IDs and function names in SRE evidence when needed. Do not include row contents or raw
customer, exchange, phone, card, or bank account identifiers.
````

</details>

It assumes that the [`axiom` CLI](https://axiom.co/docs/reference/cli) is installed. Note that it also hardcodes the path to the script that patches `convex-js`, as described in the previous section; that path is specific to my project and should likely be updated in yours. Of course, this skill could easily be adapted to take advantage of Convex MCP insights available for Convex Cloud deployments.

### Approaches to fixing specific OCC failures

#### 1. Splitting mutations

This approach is demonstrated in the second example in the section "OCC failures are surprisingly common in fairly mundane Convex code" above. An atomic mutation is split into multiple mutations that either call one another via `ctx.scheduler.runAfter(0, ...)` (but not directly via `ctx.runMutation()`! That keeps the outer mutation a single OCC conflict "target" for the Convex backend), or are called separately, one after another, from the calling action or from the client.

This is the most common way of fixing OCC failures; it helps in almost half of OCC failure cases in my experience. The typical downside is that splitting mutations often requires more explicit coordination and version/epoch/snapshot/generation checking (and storing them in the fields of documents in the first place) to avoid race conditions and [ABA problems](https://en.wikipedia.org/wiki/ABA_problem).

#### 2. Split-out and projection tables

Splitting tables simply means moving frequently updated fields from one table to a split table, such as the `ticketStateUpdates` and `accountBalanceUpdates` tables in the example above.

A projection table aggregates a few fields from different tables into a single table so that the mutation experiencing OCC failures can read fewer documents. In the same example, suppose that the failing mutation was reading `AccountBalance.balanceAmount` from the `AccountBalance` document and `TicketState.status` from the `TicketState` document. The projection table would be `someMutationInputs`, with fields `accountBalanceId`, `accountBalanceAmount`, `ticketStateId`, and `ticketStatus`; the mutations that normally update `AccountBalance.balanceAmount` would also start updating `someMutationInputs`; the failing mutation would now avoid reading the `AccountBalance` and `TicketState` documents and instead read only the relevant document from `someMutationInputs`.

Table splitting and projections are the second most common way to prevent OCC failures in my experience. To give you a sense of how common occasional OCC failures in "benign" Convex code can be, **about 30–40 tables out of about 350 tables in my application are either split-out or projection tables that wouldn't have existed if I hadn't needed to avoid OCC failures**. The schema is significantly more [snowflake-y](https://en.wikipedia.org/wiki/Snowflake_schema) (due to split-out tables) and less normalised (due to projection tables) than my application's domain logic requires, and more so than I expected, considering that Convex is a *document* database.

However, this overhead is still manageable, and I readily accept it in exchange for Convex's OCC advantages.

#### 3. Making more narrowly targeted index reads

A common theme in Convex docs and guidance is to avoid a broad `collect()`/`take()` followed by `filter()` in favour of [index reads to improve performance](https://docs.convex.dev/database/reading-data/indexes/indexes-and-query-perf). But narrower, more accurate fan-out index reads also help to avoid OCC write conflicts because they reduce the read sets that Convex mutations monitor and consider "conflicting" if any document is inserted, deleted, or updated in the read index range while the mutation is running. Read [How Convex Works](https://stack.convex.dev/how-convex-works) for more details on this.

In fact, sometimes avoiding OCC write conflicts requires pushing this "index read narrowing" beyond what might be reasonable for read performance alone. A table index with three fields may already have very narrow (no more than one or two documents) read sets in its "leaves" (that is, when all three fields are specified via `eq()`). For pure read performance, it might be sufficient to keep such an index as is and accept a `collect()` that almost always collects exactly one document, and only occasionally two or three. But these "occasional" cases are exactly when "mundane" OCC failures happen, and avoiding them requires extending the index to four or five fields, where the leaves essentially become guaranteed to contain unique documents, to avoid reading the occasional "stray" documents from the perspective of Convex's read set calculation.

#### 4. `runQuery(..., { useStaleSnapshot: true })` within a mutation

This feature was added in [Convex 1.42.0](https://github.com/get-convex/convex-js/blob/main/CHANGELOG.md#1420). It allows you to read some documents within a mutation but exclude them from the read set (monitored for OCC write conflicts) altogether. In effect, reads wrapped in `ctx.runQuery(..., { useStaleSnapshot: true })` within a mutation use [snapshot isolation rather than serializable isolation](https://brooker.co.za/blog/2024/12/17/occ-and-isolation.html).

From its description, it seems that this is what should be used to fix most OCC failures. However, in practice, it's surprisingly hard to find a good use for it. In my Convex application, I found exactly one case where this feature permitted me to replace an action—which made multiple `runQuery()` and `runMutation()` calls and choreographed them, but made no external fetches or API calls—with a single mutation containing a `runQuery(..., { useStaleSnapshot: true })` call.

#### 5. Avoiding unnecessary writes and reads

There have been a few OCC failures in my Convex application that were caused by somewhat careless `ctx.db.patch("myTable", myDocId, { someField: value })` calls on objects that *sometimes* don't need to be updated because the values in the stored document are already the same as in the patch arguments. It might be a little surprising that `convex-js` or the Convex backend doesn't already do this checking itself and thus prevent unnecessary writes.

#### 6. Sharding and striping of counter-like writes

This pattern is implemented in the [shared-counter component](https://github.com/get-convex/sharded-counter/blob/main/src/component/public.ts), although the component source code is so small that I think, rather than actually using it *as a Convex component* in your application, it makes more sense to treat it as example code and a pattern, copy it into your codebase, and adapt it to the specific use case. More often than not, multiple counters are updated at the same time; for example, account deposits and withdrawals arrive as events and we want to compute not just the count of deposits or withdrawals, but the count plus the total sum.

### Recap

Convex's OCC model is worth its trade-offs, but the default retry budget makes permanent failures too easy to hit in otherwise reasonable code. Monitor OCC failures periodically, reconstruct retry chains from logs, fix contention at its source, and adopt the [mutation retry patch](https://github.com/get-convex/convex-js/pull/170) as the safety net. The common fixes for OCC failures are smaller mutations, split-out and projection tables, narrower index reads in mutations, and removal of unnecessary reads and writes.
