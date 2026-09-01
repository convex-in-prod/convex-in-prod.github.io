---
title: "Convex self-hosting: ROI, operational lessons, configuration"
description: ""
publishDate: 2026-09-01
tags: ["self hosted", "operations", "internals"]
pinned: false
draft: false
---

I decided to migrate [my application](/posts/about-blog-and-my-app) to a self-hosted Convex server for a combination of reasons:

- I needed to call bespoke native libraries from Convex actions, which is impossible in Convex Cloud, even with the [Node.js runtime](https://docs.convex.dev/functions/runtimes#nodejs-runtime) because Convex Cloud runs Node actions in AWS Lambda using some stock container image.
- I was anxious about the rising Convex Cloud cost as my application scaled.
- I wanted to reduce the number of SaaS/cloud providers my project depends on.
- I wanted to move my application closer to important clients, while the current Convex Cloud locations (US East or Ireland) are both quite far from them.

After running a self-hosted [convex-backend](https://github.com/get-convex/convex-backend) on a server with 32 vCPUs and 64 GB of memory for one and a half months, processing tens of millions of requests, my experience has settled and my patch sets for [convex-backend](https://github.com/convex-in-prod/convex-backend/tree/main/patches) and [convex-js](https://github.com/convex-in-prod/convex-js/tree/main/patches) are fairly stable.

## Big picture observations and lessons learned

Before the migration to the self-hosted server, my Convex Cloud bill was about 600 USD per month (in Convex EU region; the equivalent of 450 USD per month in Convex US East region). I originally thought that I could serve convex-backend with my application from an 8 vCPU, 32 GB server easily, which costs about 250 USD per month on the cloud platform that I use, so I would save money right away. Boy, I was wrong.

First, I realised that **the main bottleneck for convex-backend is CPU, not memory, so I migrated to a "CPU-optimized" 16 vCPU, 32 GB server**. But even that wasn't enough, and I could only stabilise the workload on a 32 vCPU, 64 GB server, and even that was a narrow achievement! This server costs me about 800 USD per month, more than my original Convex Cloud bill (though my application's usage has grown and my Convex Cloud bill would probably be around 1500 USD per month now had I stayed with Convex Cloud).

This means that a realistic break-even threshold for self-hosting Convex to become economical is when your Convex Cloud bill grows to about 1000 USD per month. Furthermore, note that my application has little hour-to-hour or day-to-day load variation, which makes my server with Convex comparatively better utilised and thus more economical. For applications in which most activity happens in a few peak hours within each day, a self-hosted Convex server might not break even with Convex Cloud at all. And this *also* completely ignores the cost of the database cluster that should serve as the data storage for convex-backend, as discussed below in this post.

This observation means a few things. First, **Convex Cloud's markup over its actual cloud operational expenses for running workloads is probably within 20%**, and this is relatively little by DBaaS standards.

Second, **at the moment, convex-backend is actually one of the least efficient web frameworks/web app platforms.** Typically, web frameworks don't need a 32 vCPU, 64 GB server to barely sustain a workload of ~7k executions per minute, like my application (an *execution* here means any query, mutation, action, or HTTP action). I'll explain why this is the case in detail below in this post. But roughly, there are three large contributing themes:

(1) **convex-backend's implementation is very much not optimized.** Huge efficiency gains are possible either while preserving 100% of the application platform semantics or with some minuscule semantic changes that any *reasonable* Convex app should be OK with, such as requiring that different invocations of queries, mutations, and Convex-runtime actions not write to and communicate through top-level TS module variables.

(2) Convex's ubiquitous query reactivity on clients and its "bundled" design create **load spikes during Convex application deployments**. The Convex server is simultaneously the application server and its own deployment manager: it verifies deployment code, [checks that the new schema matches the data already stored on the server](/posts/schema-validation-progress-updated-occ-failures/), and so on. Sustaining these deployment spikes, as well as "natural" application load spikes on the seconds-to-tens-of-seconds timescale, requires a fair amount of overprovisioning in addition to the convex-backend and convex-js patching discussed below. Preserving a 100% success rate for the mutations, queries, and actions most critical to the application's correctness, liveness, and data persistence requires further headroom.

Although my application's hour-to-hour load variation is low, the variation on the seconds-to-tens-of-seconds timescale is high, and as a result, the server's average CPU utilisation is just about 25%. So, in some sense, my server is probably two times overprovisioned, assuming that "optimal" provisioning should yield average CPU utilisation of about 50%.

(3) **`convex-backend` is *not* designed with self-hosting (read: inelastic CPU capacity) in mind.** The clearest manifestation of this is that convex-backend sheds executions very aggressively when some execution capacity limits are exhausted. Unmistakably, this is designed for Convex Cloud, where there is some decent scale-out capacity basically for all paying customers (the [S256 deployment class](https://docs.convex.dev/production/state/limits#concurrent-function-executions) in theory permits running up to 1280 executions concurrently: 256 queries, 256 mutations, 512 Convex-runtime actions and HTTP actions combined, and 256 Node.js runtime actions). Aggressive load shedding beyond such limits is a precaution against application programming mistakes or a way to indicate that the application should be upgraded to a bigger deployment class.

A self-hosted convex-backend on a 32 vCPU server couldn't run more than about 30 *active V8 isolates* (meaning "actively executing application logic and consuming CPU, not waiting on I/O") at any moment. There is a dedicated knob in convex-backend for this limit, `FUNRUN_ISOLATE_ACTIVE_THREADS`. The total number of concurrent Convex-runtime executions could be larger than this, provided that most queries, mutations, and Convex-runtime actions spend most of their time either in database or network I/O, that is, awaiting `db.get()`, `db.insert()`, `db.patch()`, `db.delete()`, `fetch()`, etc., but still not higher than about 80 executions in total. The limiting factor is still CPU rather than memory, let alone disk I/O: Convex runtime runs executions in their own V8 contexts, and context initialisation and module loading add significant CPU overhead.

Convex Cloud's S256 deployment class permits 16 times as many concurrent executions as that! A self-hosted Convex server should be more judicious and clever with what executions it admits (to preserve 100% availability for critical executions in the face of load bursts and with such limited capacity), and also should be more lenient with how it handles transient overload, so that the success rate of the non-critical requests doesn't become trash either, at the cost of higher latency for these non-critical requests.

What I described in the previous paragraph is what `convex-backend` *should* do to be effective in a self-hosted setting, but the upstream get-convex/convex-backend *doesn't* do and doesn't even attempt to do. So, **you shouldn't think of convex-backend as a system with "first-class" self-hosted support**, like most open-source web application frameworks. It was clearly designed *directly for serverless/cloud*, with self-hosting as more of a proof-of-concept option than a production-ready feature, even apart from the fact that scaling out is explicitly not supported for self-hosted `convex-backend`.

The three themes above contribute to each other. The fact that convex-backend can't run many more executions concurrently than there are available CPUs (3) is mostly due to the lack of optimisation (1), not anything fundamental. `convex-backend` needs to be overprovisioned to handle load spikes (2) because it isn't designed to handle them gracefully (3).

The flip side is that *improving* convex-backend along these directions also incidentally helps to alleviate issues in the other themes.

Also, most of the actual issues and limitations don't run very deep in `convex-backend`'s architecture. Sometimes they are just configuration defects, sometimes they require writing some code within `convex-backend`, but not "rewriting half of the repository". GPT 5.6 Sol xhigh has fixed a good chunk of them for me, except for a few of the most challenging ones (which we are still working on), in the course of one and a half months. The current patch set is stable in production and addresses the issues well enough to handle my current workload: about 7k/min sustained executions, or more precisely: about 3.4k queries, 2.8k mutations (about 1k of which are scheduled), 300 HTTP actions, 300 scheduled Node actions, and 150 Convex-runtime actions.

**I wouldn't recommend self-hosting Convex for non-negligible production load on servers with fewer than 8 vCPUs dedicated to `convex-backend`.** This is a direct consequence of the above, and more narrowly of the third theme. convex-backend's architecture is built for serverless, not for self-hosting, and particularly not for self-hosting on a *small* server, with 4 or fewer vCPUs. This could be alleviated a bit by patches to convex-backend, but this limitation stems in large part from Convex's application programming model and thus couldn't really be fixed.

**Expect lower application availability (i.e., lower execution success rate) in the first month of self-hosting Convex.** convex-backend has [quite a lot of knobs](https://github.com/convex-in-prod/convex-backend/tree/main/self-hosted/advanced/knobs.md), and making its self-hosting version production ready (chiefly, sustaining the availability of the critical executions during transient load spikes) demanded adding yet more knobs. Getting the values for these knobs right depends on the specific application's load profile and is impossible to set optimally from the first try. Initially suboptimal configurations will lead to some extra (otherwise available) execution shedding and other failures.

I'll share my full production configuration below in this post as an example of a configuration that works fine and avoids obvious inconsistencies and footguns.

**Monitor a self-hosted Convex deployment using an SRE harness every day** to prevent outages due to exceeding the resource limits, detect misconfiguration, and catch [OCC write conflicts](/posts/occ-write-conflicts/) and app-level failures.

**A good SRE harness (logging, metrics, scripts, agent skills, alerts) is bespoke to the specific project. Thus, I cannot share the harness of my project, but I can share the "ghost SRE tooling", that is, [a very detailed checklist of the things that an SRE harness should track in self-hosted Convex servers](https://github.com/convex-in-prod/self-hosted-convex-sre) (as a minimum). This checklist could be given to an AI agent who can develop the harness for your project based on this checklist.**

## Co-located convex-backend and MySQL on a single server

I've chosen MySQL as the database backbone for my self-hosted Convex setup because of the recent praise of it by Jamie Turner, the CEO of Convex, Inc.:

<blockquote class="twitter-tweet" data-media-max-width="560"><p lang="en" dir="ltr">MySQL is still just better and people refuse to accept it<br><br>We need a Mad Men-esque reboot centered around whatever Postgres did in the early 2010s <a href="https://t.co/XwVaxSEjir">https://t.co/XwVaxSEjir</a></p>&mdash; Jamie Turner (@jamwt) <a href="https://x.com/jamwt/status/2066214900322681318?ref_src=twsrc%5Etfw">June 14, 2026</a></blockquote> <script async src="https://platform.x.com/widgets.js" charset="utf-8"></script>

<blockquote class="twitter-tweet"><p lang="en" dir="ltr">The fact that saying MySQL is fine (and always has been) is considered somewhat controversial tells you how much of the Postgres discourse is based on vibes rather than actual engineering<a href="https://t.co/MALjMAsGoW">https://t.co/MALjMAsGoW</a></p>&mdash; Jamie Turner (@jamwt) <a href="https://x.com/jamwt/status/2069480863708922094?ref_src=twsrc%5Etfw">June 23, 2026</a></blockquote> <script async src="https://platform.x.com/widgets.js" charset="utf-8"></script>

Convex Cloud was migrated from MySQL to Postgres in [July 2025](https://news.convex.dev/powered-by-planetscale-for-postgres/) and then back to MySQL during the fall of 2025 because it turned out MySQL sustains Convex's write and access patterns better than Postgres.

In addition, I've decided to co-host convex-backend and MySQL on a single server. Using a MySQL cluster like PlanetScale's Vitess behind convex-backend would only make sense if I needed very strong data durability guarantees, six nines of data durability or more. But database clusters are very expensive. If I used PlanetScale's Vitess, it would double my self-hosted Convex cost from about 800 USD to about 1600 USD per month, and would probably not break even with Convex Cloud even today, despite my relatively favourable workload.

Fortunately, my application doesn't require *extremely* high recent-data durability, so I'm fine with a single MySQL server with daily backups, rather than a cluster. In this design, **hosting convex-backend and MySQL on separate servers would not reduce the blast radius**: if either service is unavailable, the application is unavailable.

Additional reasons to co-locate self-hosted Convex and MySQL server on the same physical server/VPS are:

- Reduce the security perimeter by making MySQL completely private to the server itself.

- Reduce the network I/O of the Convex server: interactions with MySQL become server-internal loopback. The latency of queries between convex-backend and MySQL becomes lower, too.

- convex-backend and MySQL have complementary resource usage profiles: convex-backend is CPU-bound and doesn't use disk much, while MySQL uses more disk I/O and is memory-bound. Although MySQL is memory-bound, my co-located setup still uses a memory-dense server with a 1:2 vCPU:GiB memory ratio and is CPU-bound overall. This means that if I hosted convex-backend and MySQL separately, the convex-backend server would be comparatively even more CPU-bound and would "waste" even more memory. My cloud provider doesn't offer even denser CPU-to-memory setups for larger servers, and few cloud providers do.

## My complete convex-backend and MySQL server config

### Host and container capacity

- Host: 32 vCPUs, 64 GiB RAM.
- Root disk: `200 GiB`.
- Swap: disabled.
- Convex backend CPU limit: `28.5`.
- Convex backend memory limit: `31.5g`.
- MySQL CPU limit: `5`.
- MySQL memory limit: `30g`.
- MySQL CPU shares: `10463`.
- Combined CPU ceilings: `33.5`, allowing MySQL to use otherwise-idle backend CPU.

### convex-backend

##### Process pools and timeouts

- `RUNTIME_WORKER_THREADS=28` (upstream default: `0`, meaning host CPU count).

- `V8_THREADS=16` (upstream default: `0`, meaning host CPU count).

- `V8_ACTION_USER_TIMEOUT_SECS=1800`.

- `NODE_ACTION_USER_TIMEOUT_SECS=600`.

- `HTTP_SERVER_TIMEOUT_SECONDS=300`.

- `DATABASE_UDF_USER_TIMEOUT_SECONDS=5` (upstream default: `1`).

- `MYSQL_TIMEOUT_SECONDS=19`.

- `ANALYZE_CONCURRENCY=4`.

- `SCHEDULED_JOB_EXECUTION_PARALLELISM=64` (upstream default: `8`).

- `MAX_BYTES_WRITTEN_PER_SECOND=33554432`
  (32 MiB/s; upstream default: `4194304`, 4 MiB/s). This is the enforced
  write-rate ceiling. I raised it after scheduled and maintenance mutations
  were failing at `4 MiB/s` immediately after deployment with `TooManyWrites` errors.

- `PROPOSED_MAX_BYTES_WRITTEN_PER_SECOND=16777216`
  (16 MiB/s; upstream default is `1048576`, 1 MiB/s). This does not reject or
  slow writes. It only records when the write rate exceeds the configured
  value, by incrementing the `WRITE_THROUGHPUT_LIMIT_WOULD_BE_EXCEEDED_TOTAL` metric counter.

- `DOCUMENT_RETENTION_DELAY=172800`
  (2 days; upstream default is `1209600`, 14 days).
  `get-convex/convex-backend`’s example self-hosted Compose also explicitly
  supplies two days, but that is a Compose setting rather than the backend
  default.

The scheduled-job limit is intentionally much higher than the active JavaScript limit:
scheduled jobs often spend most of their lifetime waiting on external I/O.

##### Application concurrency

  The [upstream self-hosted Compose file](https://github.com/get-convex/convex-backend/blob/7ea4d77294eacfdc36328a67cb40a66d00ddce5c/self-hosted/docker/docker-compose.yml#L15) uses `16` for these limits unless overridden. That matches the [S16](https://docs.convex.dev/production/state/limits#concurrent-function-executions) deployment class for queries and mutations but is smaller than S16's `64` for both kinds of actions. In my settings, the query and mutation limits match the S256 deployment class values, while the Convex-runtime and Node action limits are half of the S256 values. *In practice, the isolate-backed limits are pointless (but also probably harmless) because de-facto the workload is limited by `MAX_ISOLATE_WORKERS`, which is smaller than any of these values; Node actions run in a separate executor and are not limited by `MAX_ISOLATE_WORKERS`.*

- `APPLICATION_MAX_CONCURRENT_MUTATIONS=256` (self-hosted Compose default: `16`).
- `APPLICATION_MAX_CONCURRENT_QUERIES=256` (self-hosted Compose default: `16`).
- `APPLICATION_MAX_CONCURRENT_V8_ACTIONS=256` (convex-backend's default: `64`; self-hosted Compose default: `16`).
- `APPLICATION_MAX_CONCURRENT_NODE_ACTIONS=128` (convex-backend's default: `64`; upstream Compose default: `16`).

Introduced in the patch about [degradable reactive queries](https://github.com/convex-in-prod/convex-backend/tree/main/patches/degradable_reactive_queries/):

- `APPLICATION_MAX_CONCURRENT_DEGRADABLE_QUERY_LEADERS=42`

##### HTTP admission

- `HTTP_SERVER_MAX_CONCURRENT_REQUESTS=192`. Upstream self-hosted Compose leaves this unset; convex-backend's default: `1024`. This is one of those cases where convex-backend's configuration seems to be wildly inconsistent internally: it doesn't make any sense to permit 16-64 mutations, queries, actions and 1024 HTTP requests.

Introduced in the patch about [shared base HTTP admission](https://github.com/convex-in-prod/convex-backend/tree/main/patches/shared_base_http_admission/):

- `HTTP_SERVER_DEPENDENCY_RESERVE=1`

##### V8 isolate workers

- `MAX_ISOLATE_WORKERS=80` (convex-backend's default: `300`).
- `ISOLATE_QUEUE_SIZE=512` (convex-backend's default: `2000`).

Introduced in the patch about [dependency capacity](https://github.com/convex-in-prod/convex-backend/tree/main/patches/dependency_capacity/):

- `ISOLATE_DEPENDENCY_WORKER_RESERVE=20`. This means that the "main" worker capacity a.k.a. the "shared (worker) base" is 80 - 20 = 60.
- `MAX_ISOLATE_ACTION_WORKERS=12`.

##### Active JavaScript admission

- `FUNRUN_ISOLATE_ACTIVE_THREADS=28`
  (backend default: `0`, meaning unlimited).

Introduced in the patch about [degradable reactive queries](https://github.com/convex-in-prod/convex-backend/tree/main/patches/degradable_reactive_queries/):

- `FUNRUN_ISOLATE_PROTECTED_ACTIVE_THREADS_MIN=4`.
- `FUNRUN_ISOLATE_DEGRADABLE_ACTIVE_THREADS_MIN=14`.

The number of elastic permits after the two floors is 28 - 14 - 4 = 10. Either class can borrow unused permits. Dependency resumptions retain priority because they unblock already-running executions.

##### Queue control

Upstream convex-backend has the so-called *CoDel queue* ("controlled delay") that prevents the V8 isolate requests (executions such as queries, mutations, actions, and the extra operations related to application deploy and schema validation) from growing uncontrollably. The default knob values pertaining to CoDel queue are:

- `CODEL_QUEUE_IDLE_EXPIRATION_MILLIS=5000`.
- `CODEL_QUEUE_CONGESTED_EXPIRATION_MILLIS=50`.

My convex-backend deployment uses the [isolate queue control](https://github.com/convex-in-prod/convex-backend/tree/main/patches/isolate_queue_control/) patch that introduces `IsolateDelayQueue` as an alternative to `CoDelQueue`. It serves the same overall function (through different mechanisms) but also differentiates V8 isolate requests by "lanes" to better cope with limited CPU resources (`MAX_ISOLATE_WORKERS` and `FUNRUN_ISOLATE_ACTIVE_THREADS`). The patch adds the following knobs:

- `ISOLATE_QUEUE_DELAY_CONTROL_ENABLED=true`. This knob controls whether `CoDelQueue` or `IsolateDelayQueue` is used. Only one of them is used. If set to `false`, all the knobs below in this list are irrelevant. If set to `true`, the two `CODEL_QUEUE_...` knobs listed above are irrelevant.
- `ISOLATE_QUEUE_DELAY_TARGET_MILLIS=750` (default in the patch: `150`).
- `ISOLATE_QUEUE_DELAY_INTERVAL_MILLIS=1000`.
- `ISOLATE_QUEUE_DELAY_SHED_THRESHOLD_MILLIS=6000` (default in the patch: `300`).
- `ISOLATE_QUEUE_HARD_MAX_AGE_MILLIS=10000` (default in the patch: `5000`).
- `ISOLATE_CONTROL_PLANE_LANE_ENABLED=true`.
- `ISOLATE_CONTROL_PLANE_QUEUE_CAPACITY=16`.
- `ISOLATE_CONTROL_PLANE_HARD_MAX_AGE_MILLIS=30000`.

The `IsolateDelayQueue` design is detailed in the patch's [design reference](https://github.com/convex-in-prod/convex-backend/blob/main/patches/isolate_queue_control/isolate_delay_queue_design_reference.md). In short, it considers four different "lanes" of isolate requests:

| Rust variant        | Metric label         | Classification                                                                                                                                                                   | Queue and scheduler policy                                                                                           |
| ------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `Dependency`        | `dependency`         | Is a child of another request: e.g., a query or a mutation called from an action, or an action called from another action                                                        | Can use dependency-only queue and worker overflow (`ISOLATE_DEPENDENCY_WORKER_RESERVE`); bypasses adaptive shedding  |
| `ControlPlane`      | `control_plane`      | The operations related to application deploy and schema analysis: `Analyze`, `EvaluateSchema`, `EvaluateAuthConfig`, `EvaluateAppDefinitions`, or `EvaluateComponentInitializer` | Uses the shared worker base, has a lane-local queue sub-cap and longer hard deadline, and bypasses adaptive shedding |
| `IndependentAction` | `independent_action` | `Action` or `HttpAction` without dependency ancestry                                                                                                                             | Uses the shared worker base, obeys the `MAX_ISOLATE_ACTION_WORKERS` cap, and participates in adaptive shedding       |
| `Ordinary`          | `ordinary`           | Every request not classified above                                                                                                                                               | Uses the shared worker base and participates in adaptive shedding                                                    |

##### Context reuse

Upstream convex-backend and convex-js support an unadvertised `experimental_reuseContext` feature: for Convex `.ts` files that have `export const experimental_reuseContext = true;` as a top-level definition, mutations and queries declared within that file will try to reuse a single cached [V8 context](https://v8.dev/docs/embed#contexts) in the V8 isolate assigned to execute the query or mutation. Reuse happens only if the context was created and used before for other queries or mutations *in the same `.ts` file* (module). To increase the proportion of query and mutation calls for which the context is reused, Convex assigns the call to a V8 isolate that already holds a context previously used for another query or mutation call within the same `.ts` file, if available.

**Context reuse for all hot modules is practically mandatory because Convex spends a lot of CPU initialising contexts and loading modules inside them for every call.** Context reuse for queries and mutations should really have been made the default because it is unsafe only in very unusual cases, such as libraries doing non-idempotent initialisation of global state.

[This patch in convex-js](https://github.com/convex-in-prod/convex-js/tree/main/patches/default_database_context_reuse/) permits making context reuse the default for queries and mutations, with a separate option for HTTP actions, rather than marking each module separately. Ordinary Convex-runtime actions still require an explicit typed `experimental_reuseContext` module marker with `actions: true`. The patch permits adding the following config in [convex.json](https://docs.convex.dev/production/project-configuration#convexjson) (this is my application's config):

```json
{
  "bundler": {
    "experimentalContextReuse": {
      "default": true,
      "httpActions": true,
      "exclusions": {
        "generated/auth.ts": "...rationale..."
      }
    }
  }
}
```

`convex/generated/auth.ts` is generated by [kitcn](https://github.com/udecode/kitcn) codegen. It loads the complete Better Auth runtime, including various global caches, and serves low-volume CRUD functions. The expected performance benefit of enabling context reuse for these queries and mutations was too small to justify completing and maintaining a review of the safety of context reuse for that module.

The accompanying [context reuse](https://github.com/convex-in-prod/convex-backend/tree/main/patches/context_reuse/) patch in convex-backend replaces a single possibly reused context per V8 isolate (whose maximum total number in the convex-backend runtime is controlled by `MAX_ISOLATE_WORKERS`) with a separate [W-TinyLFU](https://github.com/ben-manes/caffeine/wiki/Efficiency#window-tinylfu)-style cache for each V8 isolate. The patch adds the following knobs:

- `ISOLATE_CONTEXT_CACHE_PROTECTED_RESIDENTS_PER_ISOLATE=5`. This is the size of the TinyLFU's protected segment. The probationary window size in this patch is hardcoded to just 1, so the maximum total of contexts that may exist for an isolate at any time is 5+1=6.

- `ISOLATE_CONTEXT_CACHE_MAX_RESIDENTS=288`. With `MAX_ISOLATE_WORKERS=80`, the structural maximum of resident contexts is 80 * 6 = 480. But keeping all 480 contexts would be very wasteful because the "tail" (less frequently used context in less frequently reached isolates) are not used much anyway. Each context takes about 30 MB in memory, so 480 contexts would have occupied about 15 GB of server's memory. This knob is a global cap on the number of retained contexts. With this value, contexts are successfully reused in my Convex application for 96% of query and mutation calls and 99.5% of HTTP action calls. This value is most probably still not optimal for my application and it could have been significantly lower (and occupy significantly less than 9 GB of server memory) while the hit on the context reuse percentage would have been tiny, but I didn't bother to optimise this because **my (high-CPU) server is *still* bottlenecked by CPU, not memory even with context reuse enabled for all modules in my application.**

##### V8 isolate and cache memory

- `ISOLATE_MAX_USER_HEAP_SIZE=134217728` (`128 MiB`; convex-backend's default: `64 MiB`). This knob's name is misleading: it actually means the required headroom in the total V8 isolate heap allowance (`ISOLATE_MAX_USER_HEAP_SIZE + ISOLATE_MAX_HEAP_EXTRA_SIZE`) for the next execution to be able to start against this V8 isolate. If there is less memory in the V8 isolate before the next execution, convex-backend tries to free some memory in the V8 isolate's heap, through garbage collection, context reclamation (added in the [context reuse](https://github.com/convex-in-prod/convex-backend/tree/main/patches/context_reuse/) patch), and other means. If there is still less memory in the heap after these attempts, the whole V8 isolate is recreated.
- `ISOLATE_MAX_HEAP_EXTRA_SIZE=268435456` (`256 MiB`; convex-backend's default: `32 MiB`). As noted in the previous point, the name of this knob is misleading. It would be clearer if the sum of this and the previous knob were called the maximum permitted heap for V8 isolates, and the first knob to be called the required headroom for the next execution. Thus, in my application, I set the maximum V8 isolate heap size to 384 MiB, and the convex-backend's default is 96 MiB. The average memory heap sizes in my production are about 220 MiB, and the maximum observed is north of 300 MiB.
- `ISOLATE_MAX_ARRAY_BUFFER_TOTAL_SIZE=67108864` (`64 MiB`). In my application, V8 isolates use single-digit MiB of ArrayBuffers in V8 isolates, so I don't change the default setting here.
- `UDF_CACHE_MAX_SIZE=1273741824` (`1215 MiB`; convex-backend's default is 100 MiB).
- `INDEX_CACHE_SIZE=736870912` (`703 MiB`; convex-backend's default is 512 MiB).
- `FUNRUN_INDEX_CACHE_SIZE=50000000` (`50 MB`).
- `FUNRUN_MODULE_CACHE_SIZE=250000000` (`250 MB`).
- `FUNRUN_CODE_CACHE_SIZE=100000000` (`100 MB`; convex-backend's default is 500 MB).
- `SOURCE_MAP_CACHE_MAX_SIZE_BYTES=100000000` (`100 MB`).

##### Memory resilience

The [memory resilience](https://github.com/convex-in-prod/convex-backend/tree/main/patches/backend_memory_resilience/) patch does many things, including:

- Making [jemalloc](https://jemalloc.net/) the default allocator across convex-backend's own Rust logic and V8. The standard glibc allocator was wasting almost 10 GB of memory in my 30 GB convex-backend process!

- Making sure convex-backend won't start with a clearly misconfigured set of knobs within the current cgroup limit (within a docker container). This prevents shooting oneself in the foot with some combinations of knobs listed above.

- Trying to reclaim memory via restarting or killing Node.js processes, clearing V8 contexts, and ultimately shedding some external load to prevent convex-backend from reaching its cgroup memory limit and being OOM-killed.

All `LOCAL_BACKEND_*` knobs in this section are introduced in this patch. They do not exist in `get-convex/convex-backend`.

- `LOCAL_BACKEND_STARTUP_ISOLATE_MEMORY_COMMIT_PERCENT=55` (patch's default: 100, that is, no "overcommit"). `convex-backend` process startup feasibility accounting includes 55% of the aggregate configured V8 heap and ArrayBuffer ceilings. This knob doesn't reduce the runtime limits: it recognizes that every V8 isolate is
  unlikely to reach its complete heap and ArrayBuffer ceilings simultaneously.

- `LOCAL_BACKEND_NATIVE_KERNEL_MEMORY_RESERVE_BYTES=2147483648` (2 GiB). The startup calculation leaves this allowance for Rust and native allocations, allocator retention, thread stacks, page tables, and cgroup kernel memory. This knob is a bookkeeping "sink" to make `LOCAL_BACKEND_STARTUP_ISOLATE_MEMORY_COMMIT_PERCENT` reflect the actual overcommit, not an artificially constructed ratio. This knob doesn't control anything at runtime.

- `LOCAL_BACKEND_MEMORY_RECLAMATION_ENABLED=true`.

- `LOCAL_BACKEND_MEMORY_RECLAMATION_ENTER_HEADROOM_BYTES=6442450944` (6 GiB).

- `LOCAL_BACKEND_MEMORY_RECLAMATION_EXIT_HEADROOM_BYTES=8589934592` (8 GiB). When cgroup headroom falls to 6 GiB, the backend asks memory owners to discard optional warm state, including reusable V8 contexts. Reclamation remains active until headroom recovers to 8 GiB.

- `LOCAL_BACKEND_MEMORY_PRESSURE_SHEDDING_ENABLED=true`.

- `LOCAL_BACKEND_MEMORY_PRESSURE_ENTER_HEADROOM_BYTES=3221225472` (3 GiB).

- `LOCAL_BACKEND_MEMORY_PRESSURE_EXIT_HEADROOM_BYTES=5368709120` (5 GiB). If reclamation is insufficient and headroom reaches 3 GiB, the backend rejects new non-dependency ("top-level", "external") executions while still admitting dependencies (see the section "Queue control" above) of the executions that are already in progress. Admission resumes after headroom recovers to 5 GiB.

- `LOCAL_BACKEND_MALLOC_TRIM_ENABLED=false`.

- `LOCAL_BACKEND_MALLOC_TRIM_MIN_FREE_BYTES=1073741824` (1 GiB).

- `LOCAL_BACKEND_MALLOC_TRIM_COOLDOWN_SECS=300`. Explicit `malloc_trim` is available only in a glibc build. Our backend uses jemalloc, so trimming is disabled; the minimum-free and cooldown settings have no runtime effect while it remains disabled.

- `LOCAL_NODE_EXECUTOR_MEMORY_PRESSURE_MIN_RSS_BYTES=2147483648`

- `LOCAL_NODE_EXECUTOR_MEMORY_PRESSURE_GRACE_SECS=60`

##### Local Node executor

The following knobs are added in the [local Node executor resilience](https://github.com/convex-in-prod/convex-backend/tree/main/patches/local_node_executor_resilience/) patch:

- `LOCAL_NODE_EXECUTOR_MAX_OLD_SPACE_SIZE_MIB=2048`.
- `LOCAL_NODE_EXECUTOR_MAX_RSS_BYTES=3221225472`.
- `LOCAL_NODE_EXECUTOR_MAX_GENERATION_AGE_SECS=21600`.
- `LOCAL_NODE_EXECUTOR_MAX_IMPORTED_SOURCE_PACKAGES=1000`.

The following knob is added in the patch about [pinned local Node executor pools](https://github.com/convex-in-prod/convex-backend/tree/main/patches/pinned_local_node_executor_pools/):

- `LOCAL_NODE_EXECUTOR_TOTAL_RSS_BUDGET_BYTES=6442450944`.

##### jemalloc

Embedded jemalloc settings:

- `abort_conf:true`;
- `background_thread:true`;
- `narenas:32`;
- `prof:false`.

`MALLOC_CONF` can override the embedded settings.

##### MySQL connections

- `MYSQL_MAX_CONNECTIONS=128`.

### MySQL database

I use [Percona images](https://hub.docker.com/r/percona/percona-server) for MySQL 8.4 because Percona images, unlike Oracle's images, already embed jemalloc. The advantage of jemalloc in MySQL over the glibc allocator was less stark than in convex-backend, but it was still sizeable, on the order of 2-3 GB effectively saved for a 30 GB MySQL container.

##### Container allocation

- CPU limit: `5`.

- Memory limit: `30g`.

- CPU shares: `10463`.

- InnoDB buffer pool: `20G`.

- InnoDB redo capacity: `4G`.

- Binary-log retention: `172800s`.

- MySQL server-side `max_connections`: `151` (Percona image's default). Apart from convex-backend's 128 connections, there could be a few more, such as the backup process connection.

##### MySQL allocator

- Allocator: jemalloc, explicitly preloaded.

- Automatic arenas: `8`.

- Per-CPU arenas: disabled.

- Background purger threads: `1`.

- Dirty decay: `10000ms`.

- Muzzy decay: `10000ms`.

- Thread cache: enabled.

- Thread-cache maximum size class: `15` (`32 KiB`).

- Transparent huge pages disabled for allocator mappings.

Effective `MALLOC_CONF`:
`abort_conf:true,percpu_arena:disabled,narenas:8,background_thread:true,
max_background_threads:1,dirty_decay_ms:10000,muzzy_decay_ms:10000,tcache:true,
lg_tcache_max:15,thp:never,metadata_thp:disabled`.

The eight-arena setting avoids inheriting a host-derived arena count that would be excessive for a five-CPU MySQL quota.

##### MySQL server settings

- `max_allowed_packet=64M`.

- `innodb_flush_log_at_trx_commit=2` (MySQL default: `1`).

- `temptable_max_ram=1G`.

- `temptable_max_mmap=0`.

- `global_connection_memory_tracking=ON` (MySQL default: `OFF`).

- Per-connection and global connection-memory enforcement remain unlimited.

- `log_bin=ON`.

- `binlog_format=ROW`.

- `binlog_row_image=FULL`.

- `binlog_transaction_compression=ON` (MySQL default: disabled).

- `binlog_transaction_compression_level_zstd=3`.

- `sync_binlog=1`.

- `general_log=OFF`.

- `slow_query_log=OFF`.

`innodb_flush_log_at_trx_commit=2` is a deliberate durability/performance tradeoff. It risks roughly the latest second of acknowledged transactions after a kernel panic, abrupt host reset, or power loss; the one-second flush interval is not a strict upper bound. `sync_binlog=1`, which is also the MySQL 8.4 default, independently makes each binary-log commit durable, but it does not make InnoDB redo durable or eliminate that loss window.

##### MySQL exporters and auxiliary clients

- Core exporter interval: `15s`.
- Attribution exporter interval: `60s`.
- Statement exporter interval: `60s`.
- Statement exporter scrape timeout: `30s`.
- Statement exporter maximum connections: `1`.
- Statement exporter maximum idle connections: `1`.
- Statement exporter connection lifetime: `10m`.
- Statement digest text limit: `512` characters.
- Maximum returned statement digests: `100`.
- Exporter account maximum connections: `3`.
- Backup account maximum connections: `1`.
- Query text is normalized and screened before external export.
- Raw general and slow query logs are not shipped.

### Caddy

I use [Caddy](https://caddyserver.com/) as a gateway to the Convex API, Convex HTTP API, frontend bundle serving (using Vite), and the Convex dashboard. Caddy runs as a systemd service, not as a Docker container.

##### Explicit settings

- HTTPS redirect status: `308`.

- Review upstream connection cap: `4`.

- Slower external-check upstream connection cap: `2`.

- HSTS max-age: `31536000`.

- Versioned asset cache lifetime: `31536000s`.

- Versioned assets use immutable caching.

- HTML entrypoints use revalidation.

- Brotli and gzip precompressed assets enabled.

- Public metrics access blocked.

- Access logs remove query strings, request headers, response headers, and client identity fields.

The two `max_conns_per_host` values are explicit limits; Caddy otherwise allows unlimited connections for that transport.

##### Caddy settings currently left at defaults

The current Caddyfile does not set explicit values for:

- proxy request queue length;

- request or response buffer sizes;

- read or write buffer sizes;

- maximum response-header size;

- proxy flush interval;

- dial timeout;

- dial fallback delay;

- response-header timeout;

- expect-continue timeout;

- keepalive lifetime;

- idle connection count;

- idle connections per upstream host;

- maximum idle connections;

- streaming timeout;

- stream-close delay.

These inherit the selected Caddy release's defaults. The `max_conns_per_host` values limit connections, but are not complete request-queue limits.

### Sidecar and observability containers

##### Convex stack

- [Convex Dashboard](https://github.com/get-convex/convex-backend/pkgs/container/convex-dashboard)
- [Fluent Bit](https://github.com/fluent/fluent-bit) collects sanitized Convex, Caddy, Docker, systemd, and host logs and sends them to [Axiom](https://axiom.co/).
- Metrics proxy is a small local service (written in Node) that normalizes the Convex backend's Prometheus output before the OpenTelemetry Collector scrapes it.
- [cAdvisor](https://github.com/google/cadvisor) exports Docker-container CPU, memory, pressure, network, block-I/O, and lifecycle metrics.
- [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) scrapes the Convex metrics proxy, cAdvisor, Caddy, host metrics, and Fluent Bit health metrics and exports them to Axiom.

##### MySQL stack

- The core [Prometheus MySQL Exporter](https://github.com/prometheus/mysqld_exporter) instance exports MySQL global status, global variables, binary-log size, connection usage, InnoDB, and related server metrics.
- The attribution Prometheus MySQL Exporter instance is separately configured to export bounded Performance Schema, table, index, file-I/O, and memory metrics.
- [SQL Exporter](https://github.com/burningalchemist/sql_exporter) runs a small set of bounded, explicitly defined SQL metric queries, including screened statement-digest and connection-memory metrics.
- The MySQL [Fluent Bit](https://github.com/fluent/fluent-bit) instance collects sanitized MySQL, exporter, Docker, systemd, and host logs and sends them to Axiom.
- The MySQL [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) instance scrapes the three MySQL exporters and MySQL-side telemetry health endpoints and exports their metrics to Axiom.

##### Sidecar and observability settings

- Convex-stack Docker log rotation: `100m` per file, `5` files.
- MySQL-side Fluent Bit and OTel collector Docker log rotation: `100m` per file, `5` files.
- cAdvisor CPU limit: `0.5`.
- cAdvisor memory limit: `256m`.
- Host OTel collector memory limit: `512m`.
- MySQL OTel collector memory limit: `256m`.
- Fluent Bit CPU/memory: no explicit hard limit.
- Metrics proxy CPU/memory: no explicit hard limit.
- Normal metrics scrape interval: `15s`.
- Detailed MySQL attribution and statement scrape interval: `60s`.
- Host OTel memory limiter:
  - limit: `256 MiB`;
  - spike limit: `64 MiB`;
  - check interval: `1s`.
- MySQL OTel memory limiter:
  - limit: `128 MiB`;
  - spike limit: `32 MiB`;
  - check interval: `1s`.
- OTel batch timeout: `10s`.
- OTel batch size: `8192`.
- OTel batch maximum size: `8192`.
- OTel export retry:
  - initial interval: `5s`;
  - maximum interval: `30s`;
  - maximum elapsed time: `300s`.
- OTel persistent sending queue:
  - consumers: `2`;
  - queue size: `2048`.
- Fluent Bit flush interval: `1s`.
- Fluent Bit backend/database input buffers: `20MB`.
- Fluent Bit other input buffers: `10MB`.
- Fluent Bit output compression: gzip.
- Fluent Bit TLS verification: enabled.
- Fluent Bit retry limit: unlimited.

### Backups and host timers

- Daily logical MySQL backup.
- Seven daily backup snapshots retained.
- Completed binary logs archived every `15m`.
- Eight days of binary-log archive retention.
- Client-side encrypted object-storage repository.
- Backup and archive operations use bounded CPU, memory, and I/O priority.
- Backup and archive operations use a shared repository lock.
- Host disk-capacity checks run every minute.
- Backup and archive service timeout: `12h`.
