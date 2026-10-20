# meshhome.app

The project website: a home page, a privacy policy and a 404 page, as plain static files in
`public/`. No build step, no scripts, and no requests to other hosts (Poppins is served from
`public/fonts/`, under the SIL Open Font License in `public/fonts/OFL.txt`).

Preview locally:

```bash
python3 -m http.server -d site/public 8000
```

Deploy (Cloudflare Workers static assets, free plan; needs `wrangler login` once):

```bash
cd site && npx wrangler deploy
```

Screenshots in `public/img/` come from MeshHome's simulated radio with sample conversations.
When the app's look changes, retake them at 1280×800 (desktop and map) and 390×844 (phone), in
light and dark, and save them as WebP.
