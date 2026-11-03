# DIMORA — AI floor plan → 3D home designer

Upload a 2D floor plan (single floor or multi-storey). DIMORA detects the walls, rooms, doors, windows and stairs, then builds a furnished 3D home. You can walk through it, edit it, budget it in ₹, and tour it with **Aria**, the AI guide.

Version 2 moves the engine into a **Python backend** built on data structures and algorithms, and replaces Gemma 4 with **Qwen 3.5**, an open-source vision-language model (Apache-2.0).

```
 Browser (frontend/)                         Python backend (backend/)                      AI
 ───────────────────                         ─────────────────────────                      ──
 3D WebGL2 renderer, 2D SVG editor,  ─HTTP→  Flask REST API (app.py)                  ─→  Qwen 3.5
 walk mode, tour camera, panels               ├─ vision/   OpenCV: sheet split, walls,       via Ollama (local, qwen3.5:9b)
                                              │            rooms, wall thickness, PDF        or OpenRouter (hosted, qwen/qwen3.5-27b)
                                              ├─ dsa/      geometry, interval merging,
                                              │            spatial grid, room graphs
                                              ├─ planner/  rooms, doors, windows, floor
                                              │            ordering, alignment, stairs,
                                              │            graph analytics, tour order
                                              └─ ai/       prompts + JSON validation
```

## Run it

**Windows:** double-click `run.bat`. **macOS / Linux:** `./run.sh`.
Then open **http://127.0.0.1:8000**.

Or by hand:
```bash
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # optional settings
python app.py
```

Without an AI connected, everything still works. The computer vision runs on the backend, and Aria uses a rule-based parser. If the backend isn't running at all, the page falls back to a simpler in-browser engine.

## Connect the AI (Qwen 3.5)

| | Local — **Ollama** | Hosted — **OpenRouter** |
|---|---|---|
| Model | `qwen3.5:9b` | `qwen/qwen3.5-27b` (default), `qwen/qwen3.5-9b` (cheapest), `qwen/qwen3-vl-235b-a22b-instruct` (most accurate) |
| Cost | free | pay per use (a floor plan costs well under ₹1) |
| Privacy | plans never leave your PC | sent to OpenRouter |
| Needs | ~8 GB of GPU memory, or a 16 GB Mac (runs on CPU, but slowly) | an API key |
| Setup | install [Ollama](https://ollama.com), then `ollama pull qwen3.5:9b` | put `OPENROUTER_API_KEY=sk-or-…` in `backend/.env`, or paste it in ⚙ Settings |

With `DIMORA_AI_PROVIDER=auto` (the default), the backend uses Ollama when it is running and has the model. Otherwise it uses OpenRouter. ⚙ **Settings** in the app shows what is connected and has a **Test AI** button.

**Why Qwen 3.5:**
- It is open-weight under Apache-2.0.
- Every size is natively multimodal: one model reads the plan image and also chats.
- The 9B model is small enough for a laptop GPU.
- Larger sizes are available through a cheap hosted API.

**What Qwen does in DIMORA:**
- names rooms and reads the scale, doors, windows, stairs and floor titles;
- splits a sheet that holds several floors when you force a floor count;
- powers Aria's actions;
- writes the tour narration;
- matches the style of an inspiration photo;
- writes the report summary.

Qwen never edits the home directly. It returns JSON that the backend validates.

## Accuracy: what the detector handles
- **Many drawing styles:** CAD drawings, coloured or watercolour presentation plans, scans and sheets with several floors. Walls are found first (thick grey or black strokes). Text, furniture, dimension lines, grid bubbles and colour washes are ignored.
- **Open-plan spaces:** an L-shaped living/dining/kitchen is kept as one connected space, split into rectangles with no wall between them.
- **Doors and windows from the drawing:** a door is a gap with a swing arc beside it, and a window is a gap with glazing lines across it. The main entrance is the widest outside door.
- **Balconies:** a balcony is recognised by its open railing side.
- **Stairs:** found from their tread lines, even without AI.
- **Fix anything before generating:** on the detection screen you can select and move rooms; **＋ Room** (drag), **＋ Door** and **＋ Window** (click a wall) add items; **Delete** (or the Del key) removes them. You can also rename rooms and change their type in the list.
- **For the best results:**
  - Connect Qwen: it reads room names, printed dimensions and floor titles.
  - Or type the real plan width.
  - Use the original PDF or a high-resolution image.

## Multi-floor plans
You can upload any of these:
- one sheet with every floor on it;
- a multi-page PDF;
- several images at once, one per floor.

The backend then:
1. splits the sheet into one plan per floor;
2. orders the floors (Qwen's level, then the printed title, then the file name, then the position on the sheet);
3. gives plans on the same sheet the same scale;
4. aligns each floor over the one below (stair matching with the Hungarian algorithm, plus maximum footprint overlap);
5. fits a staircase so its top step lands in a room upstairs.

## REST API

| Method | Path | Does |
|---|---|---|
| GET | `/api/health` | backend version and AI status (Ollama / OpenRouter) |
| POST | `/api/plans/detect` | multipart `files[]` (PNG/JPG/WEBP/PDF), `width_m`, `floors`, `use_ai` → floors in metres plus detection overlays and a log |
| POST | `/api/plans/restack` | re-order floors, then re-align and re-connect the stairs |
| POST | `/api/analyze` | room-graph analytics: BFS order, chokepoints, Dijkstra walking distances, stair geometry, whole-house connectivity |
| POST | `/api/tour` | tour order across floors |
| POST | `/api/assistant` | Aria: `{text, home, action_doc, history}` → `{reply, actions}` |
| POST | `/api/narration` | tour narration for each room |
| POST | `/api/inspiration` | inspiration photo → style, colours, floor, finish |
| POST | `/api/report-summary` | executive summary for the report |
| POST | `/api/ai/test` | a one-line check that the AI answers |

AI endpoints return **503 `ai_unavailable`** when no model is connected; the frontend then uses its rule-based fallbacks. To choose the provider per request, send the headers `X-AI-Provider`, `X-OpenRouter-Key`, `X-OpenRouter-Model` and `X-Ollama-Model`.

## Tests
```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q             # 30 tests: DSA units + API (with Qwen mocked)
python tests/fake_ollama.py     # optional: a fake Ollama on :11434 for end-to-end runs with no GPU
```

## Project layout
```
backend/
  app.py                      Flask app: API routes + serves frontend/
  dimora_core/
    config.py                 settings (.env), room types, ids
    dsa/geometry.py           rectangles, overlap, rectangle difference, shared walls
    dsa/intervals.py          interval merging, wall lines (hash map), 1-D single-linkage clustering
    dsa/spatial.py            uniform-grid spatial hash for point → room queries
    dsa/graph.py              room-adjacency graph: sweep-line, BFS, Tarjan, Dijkstra, whole-house graph
    vision/loader.py          image/PDF decoding (OpenCV, pypdfium2)
    vision/walls.py           wall-first detection: walls, gaps → doors/windows, outline, rooms → rectangles, stairs
    vision/rooms.py           classic detector (fallback) + scale estimate
    vision/sheet.py           splitting a multi-plan sheet
    planner/layout.py         pixels → metres, room typing, snapping, doors/windows (union-find de-dup)
    planner/floors.py         floor names/levels, alignment (Hungarian matching), stair fitting
    planner/analysis.py       graph analytics and tour order
    ai/client.py              Qwen via Ollama/OpenRouter, auto-fallback, JSON extraction
    ai/services.py            prompts and validation of every AI answer
    pipeline.py               uploads → finished floors
  tests/                      pytest suite + fake Ollama server
frontend/                     the web app (HTML/CSS/JS, WebGL2 3D engine)
docs/DSA.md                   every data structure and algorithm, with complexity
samples/                      two one-floor plans to try
```

## Publish it as a website for everyone
DIMORA is one Flask app that serves both the website and the API, so it deploys as a **single web
service**. The repo includes `render.yaml` (one-click Render setup), a `Dockerfile` (for Railway, Fly.io,
Koyeb, Cloud Run or a VPS) and a `Procfile`.

**1. Get an AI key (2 min).** Sign in at openrouter.ai, then Keys → Create key. Set a **credit limit** on
the key (for example $5), because the public uses your credits. A server can't run Ollama without a GPU,
so the hosted site uses OpenRouter.

**2. Put the project on GitHub (5 min).**
- Create a free account at github.com, then New repository → name it `dimora` → Create.
- Click "uploading an existing file". Drag in everything *inside* the DIMORA folder (backend, frontend,
  docs, samples, render.yaml, Dockerfile, …), then Commit.
- ⚠ Never upload `backend/.env`. It holds your key, and the key goes into Render instead.

**3. Deploy on Render (5–10 min).**
- Sign up at render.com with your GitHub account, then New → Blueprint → choose the `dimora` repo.
- When asked for `OPENROUTER_API_KEY`, paste your key, then click Apply.
- The first build takes about 5 minutes. Your site is then live at `https://dimora-xxxx.onrender.com`.
  Share that link.
- Every change you push to GitHub redeploys the site automatically. Settings → Custom Domains lets you
  attach your own domain.

**What the free plan means.** The site sleeps after 15 minutes without visitors, and the next visitor waits
about a minute while it wakes. It also has a small CPU, so plan detection takes longer than on your PC.
The Starter instance ($7/month) stays awake and is faster.

**Public-site protection** (on by default in `render.yaml` and the Dockerfile with `DIMORA_PUBLIC=1`):
- Each visitor gets `DIMORA_AI_PER_HOUR` Qwen calls an hour on your key (default 20). The whole site gets
  `DIMORA_AI_PER_DAY` (default 500). After that DIMORA quietly continues with its rule-based engine.
- Visitors who paste their own OpenRouter key in ⚙ Settings use their own credits and aren't limited.
- Visitors can't make your server call other addresses. The Ollama-URL override is ignored.
- Uploads are capped at `DIMORA_MAX_UPLOAD_MB`.

**Other hosts.** With Docker: `docker build -t dimora . && docker run -p 8000:8000 -e OPENROUTER_API_KEY=… dimora`.
Static-only hosts (GitHub Pages, Netlify, Vercel) can serve `frontend/`, but they run the simpler
in-browser engine unless ⚙ Settings → Backend URL points at a running DIMORA backend.

Cost figures are illustrative estimates, not market quotes. Structural notes are indicative only: consult a licensed architect or engineer.
