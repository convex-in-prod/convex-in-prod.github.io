# Convex in Prod

Static technical blog for `https://convex-in-prod.github.io`.

The site uses Astro Cactus on Astro, with full-page light/dark mode, Markdown and MDX posts, Expressive Code syntax highlighting, RSS, sitemap generation, and Pagefind static search.

## Write a Post

Add a Markdown or MDX file under `content/posts/`.

```md
---
title: Your Post Title
description: A short description for search engines and link previews.
publishDate: 2026-07-09
# Add or update this after editing an already published post.
# updatedDate: 2026-07-10
tags: ["convex", "production"]
draft: false
---

Write the post here.
```

The filename becomes the URL slug. For example, `content/posts/deploy-notes.md` publishes at `/posts/deploy-notes/`.

Set `draft: true` to hide a post from production builds.

After editing an already published post, add or update `updatedDate`. The post page will show `Last updated:` next to the publish date and reading time.

## Local Development

```bash
npm install
npm run dev
```

Useful commands:

| Command | Description |
| --- | --- |
| `npm run dev` | Start the local dev server. |
| `npm run check` | Run Astro and Biome checks. |
| `npm run build` | Build the static site into `dist/` and generate the Pagefind index. |
| `npm run preview` | Preview the production build locally. |

## Publishing

Push to `main`. GitHub Actions builds the site and deploys it to GitHub Pages.

In repository settings, Pages should use **GitHub Actions** as the source.

## Immutable package archives

This repository owns `public/packages/convex/<source-sha>/convex.tgz` and the
adjacent provenance manifests. Pages serves them at
`https://convex-in-prod.github.io/packages/convex/<source-sha>/convex.tgz`.
Keep every published directory immutable, including its manifest and archive bytes.

Build new packages from an exact reviewed `convex-in-prod/convex-js` source commit
using that repository's package workflow. Verify the embedded source/upstream
provenance, package version, SHA-512 checksum and lockfile integrity, then add a
new source-SHA directory here. Source and patch-history commits belong in the SDK
repository; archive publication commits belong here. Existing consumer URLs and
integrity pins survive source history rewrites.

## Credits

This site is based on the MIT-licensed Astro Cactus theme by Chris Williams.
