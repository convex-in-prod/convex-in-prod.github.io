---
title: About the "Convex in Prod" blog and my application
description: "An introduction to Convex in Prod, the Convex application behind it, and how I use AI coding agents."
publishDate: 2026-07-11
tags: ["meta"]
pinned: true
draft: false
---

I've been running a frontend data-intensive, [majestic monolith](https://signalvnoise.com/svn3/the-majestic-monolith/) application on Convex since October 2025.

I'm starting this blog to document production-validated advice and lessons learned about using Convex efficiently for my agents, my future self, the community, and their agents.

This advice might not apply well to every project or type of Convex application.

## My Convex Application

By **frontend data-intensive** I mean that my application's web frontend has chats, feeds, and dashboards, the most frequently updated elements of which are updated about once per second.

**My application is a majestic monolith: it has a single, large "reactive core"** which is "poked" from the outside by user interactions in the frontend app, crons, and mutating webhooks from external services or workers. These crons, webhooks, and frontend interactions usually call mutations that either do all of their work immediately, kick in some internal workflows or actions, schedule one-off tasks for later within Convex, or offload tasks to services external to the Convex app. The "outros" of these workflows, actions, and externally offloaded tasks are themselves mutations that might kick off something else, ad infinitum.

This reactive core is a single Convex app, without a single [Convex component](https://www.convex.dev/components) used. As you can imagine, "everything can affect everything" in this core: abstractions shamelessly leak, and component isolation doesn't work. This reactive core heavily relies on Convex's [mutation atomicity guarantees](https://docs.convex.dev/database/advanced/occ) for correctness.

Without Convex's "atomic mutations with retries on OCC write conflicts" model, with more "standard" database locking, the application would probably either grind to a halt due to locking conflicts, or it would take an order of magnitude more engineering effort to isolate subsystems into independent services and scale them properly. So Convex really saves the day for my application's architecture. It comes with tradeoffs: frequent OCC write-conflict issues push me to make the database schema and workflow operations more complex than they would "ideally" be, but this cost is manageable. I'll write about OCC write conflicts and how to avoid them in more detail in a future post on this blog.

**Scale**: As of July 2026, my application has about 350 tables in its Convex schema. Over the last 30 days, it has had the following Convex usage metrics:

 - 47M function calls, of which 26M are due to a single, relatively cheap external HTTP API query that another service makes against my application's Convex deployment; the remaining 20M Convex function calls are more representative of the application's scale.
 - 365 GB-hours of action compute
 - 28 GB of database storage, relatively little compared with the other metrics.
 - 480 GB of database I/O: the largest and the most expensive usage category.
 - 73 GB of data egress, of which 50 GB are for log streaming to Axiom.

Convex Cloud bill: about $500 per month.

The nature of my application (and Convex's reactivity model) is such that it's hard to cut down the database I/O amplification without a major rewrite that would come with its own tradeoffs. I will cover this dilemma in another blog post. Also, I expect the application to scale soon in a way that increases database I/O two- to threefold. For these reasons, I'm planning a migration to a self-hosted Convex server.

I hope the above gives you a good idea of my application. If your Convex use case differs significantly from it, take the advice I give on this blog with a grain of salt.

## AI Use Disclosure

This blog on [convex-in-prod.github.io](https://convex-in-prod.github.io) is accompanied by code—mostly my forks of official Convex repositories with patches—in [github.com/convex-in-prod](https://github.com/convex-in-prod).

In my application development, I'm a relatively early and heavy adopter of LLM/agent coding: I started actively using agentic coding in October 2024 (with Claude Sonnet 3.5 (new), aka Claude Sonnet 3.6). I'm one of those folks who stopped writing any code by hand in December 2025, with the releases of GPT-5.2 and Claude Opus 4.5. Consequently, with the latest models, I barely review any code with my own eyes anymore. However, I heavily review code-agent loops until they cannot find any remaining defects whatsoever, which tends to produce low-bug but higher-[BDUF](https://en.wikipedia.org/wiki/Big_design_up_front) code. I barely remember the specific table and file names in my application.

This vibe-coding definitely leads to some amount of slop and unnecessary rework, but I think it's still a great deal relative to the speed of delivery that this style of development enables.

For the code that I'm sharing in [github.com/convex-in-prod](https://github.com/convex-in-prod), I will apply a slightly "less vibe-coding," more "responsible" standard than I do to my application code: I review more of that code with my own eyes and try to understand the changes more deeply than I do with the code in my application.

Still, the standard wouldn't be *that* much different. I don't assume responsibility for that code. If that is unacceptable to your "AI-coding sensibilities," you probably shouldn't use any of the code that I'm sharing in [github.com/convex-in-prod](https://github.com/convex-in-prod).

One thing I can say is that I'm running all of the code that I'm sharing in production, and it helps me solve real scalability and usability issues with Convex in my application.

In this blog itself, I'm planning to use very little, if any, AI writing, mainly to sustain my writing muscle.
