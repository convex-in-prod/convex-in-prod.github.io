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

## Credits

This site is based on the MIT-licensed Astro Cactus theme by Chris Williams.
