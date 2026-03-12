# Retro Icebreaker 🧊

A real-time sprint retrospective icebreaker — a "Guess Who Wrote It" game for distributed teams. Any number of players join a session, submit anonymous answers to a question, then try to identify who wrote what.

## Features

- **Real-time multiplayer** via WebSockets (11–20 players comfortably)
- **Host controls** all phase transitions; host can optionally play too
- **Anonymous submissions** — answers are hidden until everyone submits
- **Simultaneous reveal** with shuffled answers
- **Guessing phase** — everyone (except the author) picks who they think wrote each answer
- **Author locked out** from guessing their own answer
- **Reveal phase** — host-triggered true author reveal with guess distribution
- **End stats**: most convincing bluffer, best detective, hardest answer to identify
- **Persistent sessions** stored in SQLite (history preserved across restarts)
- **Built-in question bank** with 20 icebreaker questions + host can set a custom one

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python, FastAPI, WebSockets, SQLAlchemy |
| Database | SQLite (via aiosqlite) |
| Frontend | Vanilla JS, HTML, CSS (no build step) |
| Backend hosting | [Render](https://render.com) (free tier) |
| Frontend hosting | GitHub Pages |

---

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/retro-icebreaker.git
cd retro-icebreaker
```

### 2. Run the backend locally

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
# API runs at http://localhost:8000
```

### 3. Run the frontend locally

Just open `frontend/index.html` in your browser, or serve it:

```bash
cd frontend
python -m http.server 3000
# Open http://localhost:3000
```

The frontend defaults to `http://localhost:8000` for the backend when running locally.

---

## Deployment

### Backend → Render

1. Push this repo to GitHub.
2. Go to [render.com](https://render.com) → New → Web Service.
3. Connect your GitHub repo, set **Root Directory** to `backend`.
4. Set **Build Command**: `pip install -r requirements.txt`
5. Set **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Add a **Disk** (under Advanced): mount path `/data`, size 1 GB.
7. Add environment variable: `DATABASE_URL` = `sqlite+aiosqlite:////data/icebreaker.db`
8. Deploy. Note your Render URL (e.g. `https://retro-icebreaker-api.onrender.com`).

> **Note on free tier:** Render free tier spins down after 15 min of inactivity. The first connection after spin-down takes ~30–50s. For a scheduled retro, just open the app a minute early. Upgrade to the $7/mo Starter tier to eliminate spin-down.

### Frontend → GitHub Pages

1. In your GitHub repo, go to **Settings → Secrets and variables → Actions**.
2. Add a secret: `RENDER_URL` = `https://your-app.onrender.com` (no trailing slash).
3. Go to **Settings → Pages** → Source: **GitHub Actions**.
4. Push to `main` — the workflow in `.github/workflows/deploy.yml` builds and deploys automatically.
5. Your app will be live at `https://YOUR_USERNAME.github.io/retro-icebreaker/`.

---

## Game Flow

```
lobby → answering → reveal → [guessing → guessed → revealed] × N answers → stats
```

| Phase | Who can act |
|---|---|
| **Lobby** | Host sets question, starts game |
| **Answering** | All players submit answers; host can force-reveal |
| **Reveal** | All answers shown shuffled, no attribution; host starts guessing |
| **Guessing** | All players except the author guess; host can force-advance |
| **Guessed** | Guess distribution shown; host reveals author |
| **Revealed** | True author shown; host moves to next answer |
| **Stats** | Final scoreboard shown to all |

---

## Project Structure

```
retro-icebreaker/
├── backend/
│   ├── main.py          # FastAPI app + WebSocket hub
│   ├── models.py        # SQLAlchemy models
│   ├── game.py          # State builders for each game phase
│   ├── questions.py     # Built-in question bank (20 questions)
│   ├── requirements.txt
│   └── render.yaml      # Render deploy config
├── frontend/
│   ├── index.html
│   ├── app.js           # WebSocket client + UI rendering
│   └── style.css
├── .github/
│   └── workflows/
│       └── deploy.yml   # Auto-deploy frontend to GitHub Pages
└── README.md
```

---

## Customising Questions

Edit `backend/questions.py` to add, remove, or modify the question bank. The frontend's "roll" button draws from this list.

## Upgrading the Database

SQLite is fine for your use case. If you ever want Postgres (e.g. multiple Render instances), change `DATABASE_URL` to a `postgresql+asyncpg://...` connection string and add `asyncpg` to `requirements.txt`. The SQLAlchemy models are compatible with both.

---

## Local Development Tips

- The backend hot-reloads with `--reload` flag in uvicorn.
- Open multiple browser tabs to simulate multiple players locally.
- SQLite database is created automatically at `./icebreaker.db` on first run.
- WebSocket reconnects automatically if the connection drops.
