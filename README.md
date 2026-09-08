# MSC Library — lib.msceducation.net

Static site for the Montessori School Committee (MSC) library, Kurnool.
Sibling to [MSC-Website](https://github.com/kammaash/MSC-Website), which serves
`www.msceducation.net`. Separate repo because GitHub Pages allows one custom
domain per repository.

**Status: blank route shells.** Every HTML file here is an empty valid document,
placed so the URLs resolve. The real application is being built externally and
will be migrated into these files.

## Routes

| File | URL |
| --- | --- |
| `index.html` | `lib.msceducation.net/` |
| `dashboard/index.html` | `lib.msceducation.net/dashboard` |
| `login/index.html` | `lib.msceducation.net/login` |

The `dashboard/index.html` layout (rather than `dashboard.html`) is what makes the
clean `/dashboard` URL work on GitHub Pages without a redirect layer.

## Deployment

GitHub Pages, from the `main` branch, root folder. No build step — pushing to
`main` publishes. `CNAME` claims the custom domain; `.nojekyll` stops Jekyll from
ignoring `_`-prefixed folders, which matters when a built app (`_next/`, `_app/`)
is migrated in.

## DNS

Domain is registered at Squarespace (nameservers `nsa1-4.squarespacedns.com`).
The subdomain is one record:

    Type: CNAME   Host: lib   Value: kammaash.github.io

## Notes

- The repo is public: GitHub Pages requires it on the free plan. Source is
  readable by anyone — never commit credentials, keys, or student data.
- Pages is static hosting with no server. A real `/login` will need an external
  auth provider, or a move to a host with serverless functions (Cloudflare Pages,
  Netlify). The route is a placeholder until then.
