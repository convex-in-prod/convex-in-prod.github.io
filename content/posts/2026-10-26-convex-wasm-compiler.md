---
title: "Scaling self-hosted Convex: AOT compilation of TypeScript Convex functions into Wasm"
description: "Compiling TypeScript Convex queries and mutations into WebAssembly to reduce execution latency and memory use on a self-hosted server."
publishDate: 2026-10-26
tags: ["self hosted", "internals", "operations", "wasm"]
pinned: true
draft: false
---

## Overview

I've developed [convex-wasm-compiler](https://github.com/convex-in-prod/convex-wasm-compiler) that compiles Convex queries and mutations into WebAssembly and [the accompanying change to convex-backend](https://github.com/convex-in-prod/convex-backend/tree/main/patches/static_hermes_wasm_udfs) to execute Wasm-compiled Convex functions using [Wasmtime](https://wasmtime.dev/) fibers right on convex-backend's Tokio runtime.

**TLDR results: queries and mutations are 2 times faster on average, tails 3 times smaller.** Memory retained on the server for resident execution environments (V8 contexts vs. Wasm environments) is down **80%**.

Before this change, fast execution of Convex functions required extensive [V8 context caching and reuse](https://github.com/convex-in-prod/convex-backend/tree/main/patches/context_reuse). Without V8 context reuse, each Convex function starts with a fresh context without any modules loaded or JIT warmup done. It starts loading application modules necessary for executing the function and interpreting the function's JS code. Even relatively simple Convex functions average 100-150ms of execution time. 

The problem gets worse as the application grows and the modules get larger. Convex components remedy the uncontrolled module growth, but themselves add sufficient overhead and ceremony that I think make them a net negative for a lot of apps, definitely apps that are developed by a single developer or a couple of developers where there are no concerns of subsystem ownership, API stability, etc. I discussed my stance on the "majestic monolith" style of Convex development vs. component-heavy development in [the post about my application](https://convex-in-prod.github.io/posts/about-blog-and-my-app/).

Executing a Convex function with a reused, warmed-up V8 context that already loaded all modules and has the function's code compiled into native code via [TurboFan](https://v8.dev/docs/turbofan) often drops Convex function execution time dramatically, as much as 10 times, e.g., from 100-150ms down to 10-15ms (provided that database rows that this function reads are already cached in memory).

So, V8 context reuse is/was essential for snappy and efficient Convex apps. However, there are two issues with it.

##### Memory footprint of cached V8 contexts

First, to ensure that > 99% of executions reuse V8 contexts, each V8 isolate in the convex-backend runtime needs to maintain more than one context: it needs a separate context for each of the modules (technically, [module initialization groups](https://github.com/convex-in-prod/convex-js/tree/main/patches/default_database_context_reuse#shared-initialization-groups)) that get frequently assigned for execution on each specific V8 isolate by convex-backend. In my app's configuration of self-hosted convex-backend, I'm currently using TinyLFU caches with up to 6+1 contexts ([up from 5+1 since I posted about my self-hosted Convex config](https://convex-in-prod.github.io/posts/self-hosted-roi-lessons-configuration/#context-reuse)), and up to 400 total cached contexts, despite the fact that the peak load of my application is much lower than 400 simultaneous Convex function executions. The main reason for this relative inefficiency is that V8 contexts are pinned to V8 isolates and can't be shared between isolates.

Each V8 context takes about 35MB of memory, and this gets larger itself as the application grows: two months ago, I reported that the average V8 context retained memory was 30MB in my application.

So, these contexts add up to about **14GB** of memory on my convex-backend server.

##### V8 context initialization waves upon Convex app deployments

The second issue with V8 context reuse is that some cached contexts are invalidated when the Convex app is deployed. This can trigger a big wave of module loading and compilation activity across the server that could last for about a minute, as all the hundreds of V8 contexts are re-created and re-warmed. V8 isolates don't share optimized machine code with each other; even when serialized bytecode is cached, module initialization still happens for each V8 context in isolation.

Whether substantial V8 context invalidation happens depends on which modules were updated in the app deploy. But if the deploy updates exactly the key modules containing "hot" Convex functions that constitute a huge fraction of the total server load (and therefore each of these modules has many V8 contexts associated with it within TinyLFU caches of many different V8 isolates), a large portion of all V8 contexts might be scrapped and require re-initialization.

This wave of V8 context re-warming coincides with another wave that a Convex app deployment induces: cached query results for queries in the updated modules are invalidated and the reactive subscriptions from clients to the queries from these updated modules re-execute upon app deployment even if the data in the [read set](https://stack.convex.dev/how-convex-works#read-and-write-sets) didn't change. So, the server not only needs to compile stuff and load modules, but *also* execute more queries than in the steady state.

In practice, these post-deploy waves of work can lead to queries and mutations working slower for some time and even shedding of some executions if the overload is severe.

##### Convex's Tokio runtime worker threads contend with V8 isolate worker threads and V8 background threads

This is a more general issue of running Convex functions in V8 isolates, not an issue specific to V8 context reuse. convex-backend limits the number of V8 isolate workers that can actively execute (that is, not blocked by runtime dependencies like database reads or writes, or other I/O) via the [`FUNRUN_ISOLATE_ACTIVE_THREADS` variable](https://convex-in-prod.github.io/posts/self-hosted-roi-lessons-configuration/#active-javascript-admission). This variable should be set to the CPU limit allotted to the convex-backend container, or a value a little lower than that.

Yet these threads mostly can't be isolated (without restricting the maximum sustained throughput of the server a lot) from convex-backend's own Tokio runtime whose OS thread count is set via the [`RUNTIME_WORKER_THREADS` variable](https://convex-in-prod.github.io/posts/self-hosted-roi-lessons-configuration/#process-pools-and-timeouts), or the V8 engine's own background threads that are responsible for compilation and garbage collection in V8 contexts (whose count is set via the `V8_THREADS` variable).

In my server configuration, convex-backend's CPU limit is 28.5 CPUs, `FUNRUN_ISOLATE_ACTIVE_THREADS=28`, `RUNTIME_WORKER_THREADS=28`, and `V8_THREADS=16`. Additionally, I have several [Node execution pools](https://github.com/convex-in-prod/convex-backend/tree/main/patches/pinned_local_node_executor_pools) with 1-2 active threads each. This means that in theory, there could be at least 75 active OS threads across the processes in the container that is limited to 28.5 CPUs.

This level of oversubscription is not terrible, but it is far from optimal. Over the last week when V8 Convex query and mutation execution was active on my server, the 5-minute period with the most contention reached 29% of [CPU pressure](https://docs.kernel.org/accounting/psi.html). Of course, this happened around the time of an application deploy.

The tangible outcome of this CPU pressure is that **when the server is busy (whether due to an app deployment, or just naturally increased load), Convex function executions get slower than they are under moderate load, even separately from the effects of slower database reads/writes** (the database underneath Convex such as MySQL or PostgreSQL might itself exhibit higher CPU pressure, or its container might be throttled, during the periods of higher transaction load), higher query results cache churn, index cache churn, and V8 context cache churn.

## Why compiling TypeScript Convex functions into Wasm

Moving hot function execution from V8 to a WebAssembly runtime such as [Wasmtime](https://wasmtime.dev/) embedded in Convex's Tokio runtime is an obvious response to the issues described above. The Convex team announced that a Wasm runtime is on the agenda early in 2026. In August, a Wasm runtime was [added](https://github.com/convex/convex-backend/commit/5032d4aea79fcabbb2eafe09f2f28ee4283e6834) to the convex/convex-backend source tree. However, it was [removed the next day](https://github.com/convex/convex-backend/commit/f9bed8693ae0268c5392a84051cc1e4fa06badb4) and hasn't reappeared since. That implementation used the [QuickJS](https://github.com/quickjs-ng/quickjs) interpreter compiled into Wasm, so the Convex functions are **interpreted on the Wasm substrate, not compiled themselves**. Therefore, that implementation was very slow and wasn't meant for production.

The conventional idea (also conveyed by Convex leadership during various Q&A sessions since August) is that developers are going to write Convex functions in a compiled language that has a Wasm target, primarily Rust, to benefit from the WebAssembly runtime's performance.

I started developing a compiler for ordinary Convex functions (written in TypeScript) into WebAssembly bytecode using [Static Hermes](https://github.com/facebook/hermes/tree/static_h) (a JavaScript AOT compiler that supports a Wasm target) in late July, before the Convex team's plan to accelerate the Wasm runtime agenda became known: before August, Jamie Turner only said that a Wasm runtime would probably only arrive in 2027.

From July until now, in the whole development campaign that took almost three months, embedding the Wasmtime runtime into convex-backend was a relatively minor slice of work, totalling maybe about 10% of the total effort. In other words, just adding a Wasm runtime to convex-backend is comparatively easy, and it was relatively clear to me already from the beginning (even though I dramatically underestimated some other slices of the problem, and perhaps thought that a Wasm runtime in convex-backend would be 50% of the total effort, when in reality it ended up being only 10%, and the whole project took five times longer than I initially expected).

So, actually rewriting parts of my Convex application into Rust was already on the table back then. I didn't go that route for a combination of reasons:

- Rewriting an application in the ballpark of 350k LOC of production Convex code alone, with substantial sharing of logic with other (non-Convex) parts of the system that were running in Node.js (together with these parts, production code size was more than 0.5M LOC), seemed about as daunting as making a compiler of the existing Convex functions into WebAssembly.

  - In retrospect, rewriting even 0.5M LOC from TypeScript into Rust would probably still have taken less time than the 3-month-long compiler development project, but I didn't anticipate that at the time. On the other hand, this project would probably itself have significant risks that I don't know about now because I didn't try that, would definitely disrupt the feature delivery flow, etc.

- I was anxious about Rust compilation times. And the development loop slowdown that might have caused. I feel partially vindicated in that because since then, I've written a piece of the application fully in Rust via the [native resident](https://github.com/convex-in-prod/convex-backend/tree/main/patches/native_residents) functionality that I've added to convex-backend. This part is not a Convex action or a Convex query/mutation: this is a background Rust process that is deployed together with the Convex app and is calling Convex queries and mutations itself over loopback. This part of the application is only 30k lines of Rust, but whenever its code is changed, I feel the pain of slow Rust compilation times.

- I didn't want to invent Convex programming APIs in Rust (the equivalents of `ctx.db.query()`, `insert()`, `defineSchema()`, etc.) that would later be superseded by the Convex team's official APIs. More generally, I wanted to do something that had the potential to be complementary, rather than divergent from or competing with the future Convex developments.

- Finally, making a compiler just seemed more interesting to me than doing a Rust rewrite!

## The design of convex-wasm-compiler and convex-backend Wasm runtime

The Convex application code is unchanged: it is still TypeScript that uses the `convex-js` SDK.

##### Compilation of TypeScript Convex app code into Wasm

[convex-wasm-compiler](https://github.com/convex-in-prod/convex-wasm-compiler) orchestrates source analysis, JavaScript bundling, and native compilation. It uses [tsrs](https://github.com/oxc-project/tsrs) to identify Convex function registrations and connect exported routes to their source, and `esbuild` to resolve dependencies and translate the application’s TypeScript into bundled JavaScript. Its Rust analyzer uses [Oxc](https://github.com/oxc-project/oxc) to inspect the source and dependency graph for unsafe retained state, establishing whether initialized modules can be reused across requests. These analysis results guide admission and packaging; esbuild’s emitted JavaScript supplies the executable application code.

The compiler then prepares that JavaScript as application modules, adds a bridge to Convex’s host operations (here and below, "host" means convex-backend's runtime operations like database reads and writes, "guest" means application function execution), and invokes (Static) [Hermes](https://github.com/facebook/hermes). Its `shermes` compiler emits C, which [Emscripten](https://github.com/emscripten-core/emscripten)’s Clang/LLVM toolchain compiles alongside the Hermes runtime and Convex’s native adapter. `wasm-ld` links these components, and [Binaryen](https://github.com/webassembly/binaryen) optimizes the resulting Core WebAssembly. The linked Hermes runtime supplies JavaScript objects, strings, promises, exceptions, and garbage collection inside the Wasm guest.

##### Async JS functions on Wasmtime fibers

Static Hermes compiles JavaScript async functions into resumable state machines backed by promises. A [Hermes patch](https://github.com/convex-in-prod/hermes/commits/static_h/) moves the core promise machinery into C++: it introduces garbage-collected `JSPromise` objects to hold promise state, while the [C++ Promise implementation](https://github.com/convex-in-prod/hermes/blob/static_h/lib/VM/JSLib/NativePromise.cpp) handles resolution, rejection, and continuation bookkeeping. This reduces the JavaScript object manipulation and callback allocation involved in asynchronous execution. Some higher-level Promise operations still use Hermes’s internal JavaScript implementation.

Convex’s C++ adapter, compiled into WebAssembly alongside Hermes and the application, supplies the loop that drives each invocation to completion. It calls the handler, drains Hermes’s microtask queue to run ready continuations, waits for database reads, writes, and other host operations, and resolves or rejects their promises as results arrive. When waiting requires asynchronous host work, Wasmtime suspends the invocation’s fiber, freeing the Tokio worker thread to run other tasks. Execution resumes in that same fiber when results become available. The exported Wasm entry point therefore returns a final invocation status through an ordinary call-and-return interface; JavaScript promises and continuations remain inside the guest.

Convex’s Tokio runtime polls Wasmtime’s `call_async`, which executes the guest entry point on a single Wasmtime fiber. Fuel and epoch checks enforce execution limits and provide opportunities for CPU-bound guest work to yield.

##### Compilation of Wasm into native code and deployment

In this setup, native compilation happens on the developer machine or in CI. Wasmtime’s [Cranelift](https://cranelift.dev/) compiler translates the `.wasm` modules—including the application, Hermes runtime, and Convex adapter—into machine code for the server’s target architecture and Wasmtime configuration. Wasmtime stores this code and its loading metadata in `.cwasm` files. Deployment transfers these files to the server together with the portable `.wasm` files, alongside the ordinary Convex JavaScript bundle and schema.

On the server, convex-backend checks that the compiled files are compatible with its Wasmtime engine and belong to the JavaScript application version being deployed. Recorded content hashes prevent accidentally pairing new JavaScript with an older compiled build. convex-backend loads the compiled code into memory and prepares reusable, isolated execution environments containing a Hermes runtime and the application’s JavaScript state. Module initialization (the top-level code that creates module objects and registers exported functions) completes for this initial pool before the deployment makes the new application version available to queries and mutations. The pool can grow later as demand changes. This part directly addresses the "V8 context initialization waves upon Convex app deployments" issue in the V8 execution path discussed above in this post.

##### Wasm environments and memory management

Memory savings of the Wasm runtime vs. V8 runtime come from sharing compiled code and initialized application memory across a pool of isolated execution environments. These environments are akin to V8 isolates in Convex's V8 runtime, but they don't have dedicated OS threads. Their population is controlled by an aggregate memory budget and a hard instance-count ceiling; a separate CPU-concurrency limit bounds active execution.

Each environment is managed by a Wasmtime Store. It contains a Hermes runtime and its own JavaScript state and handles one Convex execution at a time. Environments using the same compiled modules share their machine code. On Linux, convex-backend also captures an environment’s initialized Wasm memory, including its JavaScript heap, into an immutable, file-backed snapshot. This happens after application modules have initialized but before that environment has executed any query or mutation. **Other environments map the snapshot using copy-on-write**: unchanged pages share physical memory, while writes create private copies. Each environment rebuilds its own function tables and runtime references and keeps Convex function execution-specific state private. Before reusing an environment, convex-backend checks that the configuration and other dependencies read during initialization remain valid.

Hermes's [Hades](https://github.com/facebook/hermes/blob/main/doc/Hades.md) garbage collector is [patched](https://github.com/convex-in-prod/hermes/commits/static_h/) to preserve that memory sharing. In this Wasm build, Hades runs single-threaded inside guest calls. Prepared heap regions are excluded from subsequent allocation and compaction, reducing writes into the shared baseline while retaining normal tracing, sweeping, and finalization. At safe invocation boundaries, collection and allocator inspection identify empty nursery space and proven-free payload pages whose private backing the host can discard. The backend accounts for shared backing once and reserves memory for each environment's private state and growth. Wasmtime hooks track writes and memory growth, so the aggregate Wasm memory budget remains enforced during invocation execution as well as environment admission.

Wasmtime also uses a deferred reference-counting collector for engine-owned references, including caught Wasm exceptions. This is separate from Hades, which manages JavaScript objects inside linear memory. Together with bounded instance retention and page reclamation, these mechanisms let successful invocations reuse initialized runtimes without indefinitely retaining all memory touched by earlier Convex function executions on the given environment.

![Convex TypeScript function compilation into WebAssembly, deployment, and runtime architecture](/images/convex-wasm-architecture.png)

## Wasm vs. V8: performance results

The overall **mean query and mutation execution latency is down almost 50% on Wasm vs. V8 baseline:**

| Runtime      | Mean     | p90   | p99    | Max      |
| ------------ | --------:| -----:| ------:| --------:|
| V8           | 38.53 ms | 62 ms | 329 ms | 7,463 ms |
| Wasm         | 19.61 ms | 34 ms | 139 ms | 2,242 ms |

**On average, each Wasm environment retains 12MB of fully private, non-shared memory, which is a 66% decrease relative to V8 contexts' 35MB.** Also, many fewer Wasm environments should be retained (see the discussion in the "Memory footprint of cached V8 contexts" section above for why more V8 contexts should be retained than concurrent executions at any given time): **130 Wasm environments vs. about 400 V8 contexts.**

**In the five-minute period with the most contention, CPU pressure reached 15% when Wasm execution was primary, vs. 29% for V8.** Even that 15% was probably in part due to the retained V8 execution: as I will discuss below, I don't compile my whole Convex application into Wasm, but only about the hottest 20% of modules (90% of the total load), to save on compilation times and the artifact size. The remaining 80% of modules are still executed on the V8 runtime, so they are still subject to context invalidation, initialisation, still have active V8 background threads, etc.

The following two tables show average relative reductions in mean/p90/p99/max metrics within various specific queries and mutations, *not* weighted by the number of calls or total execution time each function receives.

This is the execution latency:

| Population                         | Mean   | p90    | p99    | Max    |
| ---------------------------------- | ------:| ------:| ------:| ------:|
| All queries                        | −57.6% | −56.3% | −65.3% | −82.2% |
| Queries: most-host-operation 20%   | −39.3% | −37.6% | −48.4% | −82.2% |
| Queries: fewest-host-operation 20% | −69.0% | −61.3% | −78.0% | −84.0% |
| All mutations                      | −51.5% | −33.7% | −60.6% | −75.1% |
| Mutations: most-operation 20%      | −39.4% | −43.6% | −48.9% | −70.5% |
| Mutations: fewest-operation 20%    | −51.2% | −55.7% | −61.5% | −70.6% |

This is "user" time (that is, an estimate of how much time functions spent executing in the guest runtime, excluding the time they waited for database reads, writes and other async host operations):

| Population                      | Mean   | p90    | p99    | Max    |
| ------------------------------- | ------:| ------:| ------:| ------:|
| All queries                     | −70.6% | −66.9% | −76.1% | −79.8% |
| Queries: most-operation 20%     | −43.5% | −41.0% | −56.6% | −89.0% |
| Queries: fewest-operation 20%   | −89.5% | −82.9% | −86.6% | −69.8% |
| All mutations                   | −67.7% | −46.5% | −72.5% | −59.9% |
| Mutations: most-operation 20%   | −41.0% | −41.2% | −47.7% | −39.0% |
| Mutations: fewest-operation 20% | −76.9% | −78.6% | −82.2% | −55.0% |

The "most-host-operation 20%" and "fewest-host-operation 20%" mean that these functions, on average, perform more (or fewer) async operations such as database reads and writes, relative to other functions. E.g., a function that has a single `ctx.db.get(id)` does a single async operation and therefore would be included in the "fewest 20%" lines, and a function that on average does hundreds of `ctx.db`, `ctx.scheduler`, etc. operations would be included in the "most 20%" lines.

These tables show that the relative advantage of Wasm over V8 tends to become smaller for functions that do *a lot* of async operations. This might point to a relative weakness of the JS Promise handling within a Wasmtime fiber, discussed in the "Async JS functions on Wasmtime fibers" section above. This is despite the fact that it has already received a lot of attention and I don't know (ok, ok, GPT-Astra doesn't know) how to make it any faster. Or, it might be due to the fact that a lot of Wasmtime fiber yields and thread switches to other Tokio tasks cause the Convex execution to switch hardware cores more and thus incur more L1 cache or TLB misses. Or, it might be due to something else. This is yet undiagnosed.

Also, the "user" time advantage is not universal. There is a single Convex mutation that averages 14% higher user time with the Wasm runtime than with the V8 runtime (but is still faster than V8 in terms of the overall execution latency). This mutation performs numeric operations in a loop on a `Float64Array` of more than 100k elements. This might indicate that V8 has some intrinsics or vectorisation implemented that are still lacking in the Hermes-to-Wasm pipeline.

## Operational considerations of Wasm AOT compilation

##### Shadow execution for always-on testing

I've added an entire complex subsystem to convex-backend that supports [differential testing](https://en.wikipedia.org/wiki/Differential_testing) and shadow execution of Convex functions on V8 and Wasm runtimes: it supports so-called "primary" and "shadow" execution (either V8 or Wasm could be primary or shadow) with a certain randomly sampled proportion of executions, e.g., 5%, selected for shadow execution and then validates that the results are identical. 

Shadow executions don't block the primary executions. Shadow execution is supported for both queries and mutations: for mutations, the resulting trace of state-changing calls like `db.insert()` is taken, but the actual rows in the database are not overwritten the second time, of course. Primary and shadow runtimes could be switched without application re-deploy.

This system permitted me to switch the production to Wasm with confidence, and to find a few bugs in convex-wasm-compiler. However, the majority of bugs were in the shadow execution and trace comparison/validation subsystem itself. **I also keep V8 execution at 5% shadow execution even after switching primary to Wasm, to monitor for discrepancies, if they arise in the future.**

##### Selective Wasm compilation

The compiler and convex-backend both support selective Wasm compilation: only a subset of modules are compiled and executed using the Wasm runtime. This is to aid both compilation times and transfer size during deployments. In my application, I select only about 20% of modules that are responsible for more than 90% of all query and mutation executions. However, this doesn't result in five times smaller deployment transfers and five times faster compilations: due to transitive dependencies, about 70% of all modules of my application are still subject to compilation and transfer. So, maybe this is not such a great deal.

##### Compilation times and transfer sizes

The selection of 20% of modules in my Convex application amounts to 57 modules, 631 Convex functions to be compiled into Wasm: 145 queries and 486 mutations. 528 distinct JS modules are compiled in total (57 selected modules plus their dependencies).

For this selection, cold compilation (most or all code needs to be re-compiled, e.g., after the compilation cache is cleared) with 16 AOT compilation workers and 12GB of memory allowance completes in about **6 minutes**. Warm compilation (some modules have changed, but most are cached) completes in about **2 minutes**. This is far from ideal, and I'm still working on it.

I didn't expect when I began this project that **compilation speed, incrementality, and caching would turn out to be *the* major engineering challenges.** Of about 3 months of working on this project, AI agents spent about 3 weeks of calendar time in total exclusively working in this area.

Deployments with this compiled module selection transfer about 50MB of compressed artifacts to my self-hosted Convex server. Without Wasm, it's about 15MB, so **Wasm approximately triples the deployment transfer size.** If I didn't use selective compilation, it would probably be about 5 times as large as a JS bundle-only deployment.
