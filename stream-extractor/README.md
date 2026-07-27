# Stream URL Extractor

A small FastAPI app that takes a short link / redirect link / video page URL,
follows redirects, scans the page for embedded `.m3u8` (HLS) or `.mpd` (DASH)
manifest URLs, validates that they actually respond, and lets you preview the
result in an in-browser HLS player.

**Scope note:** this tool only surfaces information a browser's network tab
already exposes — redirect targets, response headers, and URLs literally
present in a page's HTML/JS. It does not defeat DRM (Widevine / FairPlay /
PlayReady), does not fabricate or brute-force auth tokens, and does not
bypass logins or paywalls. Use it on your own content or links you have the
right to test.

## Project layout

```
stream-extractor/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI routes
│   └── extractor.py      # redirect-following, parsing, validation logic
├── templates/
│   └── index.html         # single-page UI (Tailwind + hls.js)
├── static/                 # (empty — reserved for any extra assets)
├── requirements.txt
└── README.md
```

## How it works

1. **Follow redirects** — `httpx.AsyncClient(follow_redirects=True)` walks
   through the short link with a realistic desktop User-Agent, recording
   every hop in `redirect_chain`.
2. **Check the final response** — if its `Content-Type` is
   `application/x-mpegurl` / `application/vnd.apple.mpegurl` /
   `application/dash+xml` (or the URL itself ends in `.m3u8`/`.mpd`), it's
   already a direct manifest — done.
3. **Otherwise, scan the page** — the HTML body is parsed two ways:
   - BeautifulSoup scans `<source>`, `<video>`, `<a>`, `<iframe>`, and any
     `data-*` attribute for stream-looking URLs.
   - A regex pass over the raw text catches URLs embedded in inline
     `<script>` JS or JSON config blobs.
   - Relative and protocol-relative URLs are resolved against the final
     page URL, so relative paths and query-string auth tokens survive.
4. **Validate candidates** — each candidate gets a `HEAD` request (falling
   back to a small ranged `GET`, since many CDNs reject `HEAD`). Only
   candidates that come back `200`/`206` are considered "found".
5. **Return JSON** — the first working candidate, its type (`hls`/`dash`),
   the redirect chain, and any other candidates found (for transparency
   when the "best" pick is wrong).

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000** — paste a URL, click **Extract Stream URL**.

### API only

```bash
curl -X POST http://localhost:8000/api/extract \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/watch/some-video"}'
```

Response shape:

```json
{
  "success": true,
  "message": "Found and validated a working HLS stream URL.",
  "original_url": "https://example.com/watch/some-video",
  "final_page_url": "https://example.com/watch/some-video",
  "redirect_chain": ["https://example.com/watch/some-video"],
  "stream_url": "https://cdn.example.com/live/channel1.m3u8?auth=xyz",
  "stream_type": "hls",
  "elapsed_ms": 842,
  "alternates": []
}
```

## Deployment

**Important:** this is a full-stack app — a Python (FastAPI) backend plus an
HTML/JS frontend. Static-file hosts like **Netlify, Vercel (static), and
GitHub Pages cannot run the Python backend.** If you deploy
`templates/index.html` to one of those on its own, the page will load but
every extraction attempt will fail with a network/CORS error, because
`/api/extract` doesn't exist anywhere.

You have two options:

### Option A — deploy both together (simplest)

Deploy the whole `stream-extractor/` folder to a platform that runs Python
(Render or Koyeb, see below). FastAPI serves both the API *and* the HTML
page from the same origin, so no configuration is needed — the page's
relative `/api/extract` calls just work.

### Option B — split deployment (static frontend + separate backend)

If you specifically want the frontend on a static host (e.g. Netlify) and
the backend elsewhere:

1. Deploy `app/` (the FastAPI backend) to Render or Koyeb as described
   below. Note its public URL, e.g. `https://your-app.onrender.com`.
2. Deploy `templates/index.html` (and `static/`) to your static host as-is.
3. Open the deployed static page, click **⚙ Backend URL** in the top-right
   corner, and paste the backend's URL. It's saved in the browser's
   `localStorage`, so you only set it once per browser/device.
4. The page will then call `https://your-app.onrender.com/api/extract`
   instead of a relative path. CORS is already wide open in `app/main.py`
   (`allow_origins=["*"]`), so no backend changes are needed.

If the page detects it's running on a known static host (`*.netlify.app`,
`*.vercel.app`, `*.github.io`, `*.pages.dev`) and no backend URL is set
yet, it shows a banner reminding you to configure one.

### Render

1. Push this folder to a GitHub repo.
2. On Render: **New → Web Service** → connect the repo.
3. **Build command:** `pip install -r requirements.txt`
4. **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Deploy. Render auto-detects Python and injects `$PORT`.

### Koyeb

1. Push to GitHub (or use the Koyeb CLI to deploy directly).
2. On Koyeb: **Create App → GitHub** → select the repo.
3. Runtime: **Python (Buildpack)** — Koyeb auto-installs
   `requirements.txt`.
4. **Run command:** `uvicorn app.main:app --host 0.0.0.0 --port 8000`
5. Expose port `8000`, deploy.

### Docker (works on either platform, or anywhere else)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t stream-extractor .
docker run -p 8000:8000 stream-extractor
```

## Known limitations

- **JS-rendered players:** if a site builds its manifest URL only via a
  client-side XHR/fetch call after page load (rather than embedding it in
  the initial HTML), this tool won't see it — it doesn't execute
  JavaScript. A headless-browser approach (Playwright/Selenium) would be
  needed for those, at the cost of much higher latency and resource use.
- **Referer/cookie-gated CDNs:** some manifests validate the `Referer`
  header or a session cookie before serving `200`. The extractor sends a
  generic browser UA but not the origin site's cookies, so a small number
  of otherwise-valid candidates may fail validation here even though
  they'd work when requested directly from the source page's own player.
- **DASH preview:** the in-browser test player uses hls.js, which only
  handles `.m3u8`. `.mpd` results are returned and validated the same way,
  but for in-browser preview you'll want a DASH player such as
  [Shaka Player](https://github.com/shaka-project/shaka-player) or
  [dash.js](https://github.com/Dash-Industry-Forum/dash.js).