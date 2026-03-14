import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from models import Base, Session, Player, Answer, Guess, new_id
from game import (
    get_session, get_all_players, get_active_players, get_answers,
    build_lobby_state, build_answering_state, build_reveal_state,
    build_guessing_state, build_guessed_state, build_revealed_state, build_stats_state,
    unique_name,
)
from questions import QUESTION_BANK

FRONTEND_URL = os.getenv("FRONTEND_URL", "*")

_db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
if ":memory:" in _db_url:
    engine = create_async_engine(
        _db_url, echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_async_engine(_db_url, echo=False)

DATABASE_URL = _db_url
async_session = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        # session_id → {player_id: websocket}
        self.connections: Dict[str, Dict[str, WebSocket]] = {}
        # session_id → shuffled answer order (list of answer IDs)
        self.answer_order: Dict[str, List[str]] = {}
        # session_id → asyncio.Task for the running guess timer
        self.timer_tasks: Dict[str, asyncio.Task] = {}

    def connect(self, session_id: str, player_id: str, ws: WebSocket):
        self.connections.setdefault(session_id, {})[player_id] = ws

    def disconnect(self, session_id: str, player_id: str):
        if session_id in self.connections:
            self.connections[session_id].pop(player_id, None)

    async def send_to(self, session_id: str, player_id: str, data: dict):
        ws = self.connections.get(session_id, {}).get(player_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def broadcast(self, session_id: str, data: dict, exclude: str = None):
        for pid, ws in list(self.connections.get(session_id, {}).items()):
            if pid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def broadcast_state(self, db: AsyncSession, session_id: str, session: Session):
        order = self.answer_order.get(session_id, [])
        for pid in list(self.connections.get(session_id, {}).keys()):
            state = await build_state_for(db, session, pid, order)
            await self.send_to(session_id, pid, state)

    def cancel_timer(self, session_id: str):
        task = self.timer_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    def start_timer(self, session_id: str, seconds: int):
        self.cancel_timer(session_id)
        if seconds > 0:
            task = asyncio.create_task(self._run_timer(session_id, seconds))
            self.timer_tasks[session_id] = task

    async def _run_timer(self, session_id: str, seconds: int):
        """After `seconds`, auto-advance guessing → guessed."""
        await asyncio.sleep(seconds)
        async with async_session() as db:
            session = await get_session(db, session_id)
            if session and session.state == "guessing":
                session.state = "guessed"
                await db.commit()
                await self.broadcast_state(db, session_id, session)


manager = ConnectionManager()


async def build_state_for(db: AsyncSession, session: Session, viewer_id: str, order: list) -> dict:
    if session.state == "lobby":
        return await build_lobby_state(db, session)
    elif session.state == "answering":
        return await build_answering_state(db, session, viewer_id)
    elif session.state == "reveal":
        return await build_reveal_state(db, session)
    elif session.state == "guessing":
        return await build_guessing_state(db, session, viewer_id, order)
    elif session.state == "guessed":
        answer_id = order[session.current_answer_index] if order else None
        if answer_id:
            return await build_guessed_state(db, session, answer_id, order)
    elif session.state == "revealed":
        answer_id = order[session.current_answer_index] if order else None
        if answer_id:
            return await build_revealed_state(db, session, answer_id, order)
    elif session.state == "stats":
        return await build_stats_state(db, session)
    return {"type": "error", "message": "Unknown state"}


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------

@app.get("/questions/random")
async def random_question():
    return {"question": random.choice(QUESTION_BANK)}


@app.post("/sessions")
async def create_session(body: dict):
    question = body.get("question", random.choice(QUESTION_BANK))
    host_name = body.get("host_name", "Host")
    host_is_player = body.get("host_is_player", True)
    guess_timer_seconds = int(body.get("guess_timer_seconds", 30))
    exclude_revealed = body.get("exclude_revealed_from_guessing", False)

    async with async_session() as db:
        session_id = new_id()
        host_id = new_id()

        session = Session(
            id=session_id,
            question=question,
            host_id=host_id,
            host_is_player=host_is_player,
            state="lobby",
            guess_timer_seconds=guess_timer_seconds,
            exclude_revealed_from_guessing=exclude_revealed,
        )
        db.add(session)

        host_player = Player(
            id=host_id,
            session_id=session_id,
            name=host_name,
            is_host=True,
            is_active=host_is_player,  # inactive if host-only
        )
        db.add(host_player)
        await db.commit()

    return {"session_id": session_id, "player_id": host_id, "is_host": True}


@app.post("/sessions/{session_id}/join")
async def join_session(session_id: str, body: dict):
    desired_name = body.get("name", "Player").strip()
    async with async_session() as db:
        session = await get_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        # Allow joining in lobby OR in-progress (for reconnects without a stored player_id)
        if session.state not in ("lobby", "answering", "reveal", "guessing", "guessed", "revealed"):
            raise HTTPException(400, "Session has ended")

        all_players = await get_all_players(db, session_id)
        existing_names = [p.name for p in all_players]
        name = unique_name(desired_name, existing_names)

        player_id = new_id()
        player = Player(id=player_id, session_id=session_id, name=name, is_host=False)
        db.add(player)
        await db.commit()

    return {"session_id": session_id, "player_id": player_id, "is_host": False, "name": name}


@app.get("/sessions/{session_id}/inactive_players")
async def get_inactive_players(session_id: str):
    """Return disconnected players for the rejoin prompt."""
    async with async_session() as db:
        session = await get_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        all_players = await get_all_players(db, session_id)
        inactive = [
            {"id": p.id, "name": p.name, "is_host": p.is_host}
            for p in all_players
            if not p.is_active
        ]
    return {"inactive_players": inactive}


@app.post("/sessions/{session_id}/rejoin/{player_id}")
async def rejoin_session(session_id: str, player_id: str):
    """Confirm a rejoin — just validates the player exists in this session."""
    async with async_session() as db:
        session = await get_session(db, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        result = await db.execute(
            select(Player).where(Player.id == player_id, Player.session_id == session_id)
        )
        player = result.scalar_one_or_none()
        if not player:
            raise HTTPException(404, "Player not found in this session")
        is_host = session.host_id == player_id
    return {
        "session_id": session_id,
        "player_id": player_id,
        "is_host": is_host,
        "name": player.name,
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/{session_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, player_id: str):
    await websocket.accept()
    manager.connect(session_id, player_id, websocket)

    async with async_session() as db:
        session = await get_session(db, session_id)
        if not session:
            await websocket.send_json({"type": "error", "message": "Session not found"})
            await websocket.close()
            return

        result = await db.execute(select(Player).where(Player.id == player_id))
        player = result.scalar_one_or_none()
        if not player:
            await websocket.send_json({"type": "error", "message": "Player not found — the session may have restarted. Please rejoin via the link."})
            await websocket.close()
            manager.disconnect(session_id, player_id)
            return

        # Reactivate on connect (handles rejoin)
        is_host_only = player.is_host and not session.host_is_player
        if not is_host_only:
            player.is_active = True
            await db.commit()

        order = manager.answer_order.get(session_id, [])
        initial_state = await build_state_for(db, session, player_id, order)
        await websocket.send_json(initial_state)

        # If reconnecting into an active guessing round, send timer_start BEFORE
        # broadcast_state so the client has timerEndsAt set before it renders
        if session.state == "guessing" and session.guess_timer_seconds > 0:
            task = manager.timer_tasks.get(session_id)
            if task and not task.done():
                await websocket.send_json({
                    "type": "timer_start",
                    "seconds": session.guess_timer_seconds,
                })

        await manager.broadcast_state(db, session_id, session)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            await handle_message(session_id, player_id, msg)
    except WebSocketDisconnect:
        manager.disconnect(session_id, player_id)
        async with async_session() as db:
            result = await db.execute(select(Player).where(Player.id == player_id))
            player = result.scalar_one_or_none()
            if player:
                player.is_active = False
                await db.commit()
            session = await get_session(db, session_id)
            if session:
                await manager.broadcast_state(db, session_id, session)


# ---------------------------------------------------------------------------
# Message Handler
# ---------------------------------------------------------------------------

async def handle_message(session_id: str, player_id: str, msg: dict):
    action = msg.get("action")

    async with async_session() as db:
        session = await get_session(db, session_id)
        if not session:
            return

        is_host = session.host_id == player_id

        # --- Submit or update answer ---
        if action == "submit_answer" and session.state == "answering":
            text = msg.get("text", "").strip()
            if not text:
                return
            result = await db.execute(
                select(Answer).where(Answer.session_id == session_id, Answer.player_id == player_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                # Update existing answer (change answer feature)
                existing.text = text
            else:
                db.add(Answer(session_id=session_id, player_id=player_id, text=text))
            await db.commit()

            players = await get_active_players(db, session_id)
            answering_players = [p for p in players if not (p.is_host and not session.host_is_player)]
            answers = await get_answers(db, session_id)
            submitted_ids = {a.player_id for a in answers}
            all_submitted = all(p.id in submitted_ids for p in answering_players)

            await manager.broadcast_state(db, session_id, session)
            if all_submitted:
                await manager.broadcast(session_id, {"type": "all_submitted"})

        # --- Host: start answering ---
        elif action == "start_answering" and is_host and session.state == "lobby":
            session.state = "answering"
            await db.commit()
            await manager.broadcast_state(db, session_id, session)

        # --- Host: reveal all answers ---
        elif action == "reveal_answers" and is_host and session.state == "answering":
            answers = await get_answers(db, session_id)
            shuffled_ids = [a.id for a in answers]
            random.shuffle(shuffled_ids)
            manager.answer_order[session_id] = shuffled_ids
            session.state = "reveal"
            await db.commit()
            await manager.broadcast_state(db, session_id, session)

        # --- Host: start guessing phase ---
        elif action == "start_guessing" and is_host and session.state == "reveal":
            session.state = "guessing"
            session.current_answer_index = 0
            await db.commit()
            # Start timer BEFORE broadcast_state so clients have timerEndsAt set before rendering
            if session.guess_timer_seconds > 0:
                manager.start_timer(session_id, session.guess_timer_seconds)
                await manager.broadcast(session_id, {
                    "type": "timer_start",
                    "seconds": session.guess_timer_seconds,
                })
            await manager.broadcast_state(db, session_id, session)

        # --- Submit guess ---
        elif action == "submit_guess" and session.state == "guessing":
            order = manager.answer_order.get(session_id, [])
            if not order:
                return
            answer_id = order[session.current_answer_index]

            result = await db.execute(select(Answer).where(Answer.id == answer_id))
            answer = result.scalar_one_or_none()
            if not answer or answer.player_id == player_id:
                return

            # Prevent duplicate guess
            existing = await db.execute(
                select(Guess).where(Guess.answer_id == answer_id, Guess.guesser_id == player_id)
            )
            if existing.scalar_one_or_none():
                return

            guessed_pid = msg.get("guessed_player_id")
            is_correct = guessed_pid == answer.player_id

            db.add(Guess(
                session_id=session_id,
                answer_id=answer_id,
                guesser_id=player_id,
                guessed_player_id=guessed_pid,
                is_correct=is_correct,
            ))
            await db.commit()

            players = await get_active_players(db, session_id)
            eligible = [p for p in players if p.id != answer.player_id]
            guesses_result = await db.execute(select(Guess).where(Guess.answer_id == answer_id))
            guessed_ids = {g.guesser_id for g in guesses_result.scalars().all()}
            all_guessed = all(p.id in guessed_ids for p in eligible)

            await manager.broadcast_state(db, session_id, session)

            if all_guessed:
                manager.cancel_timer(session_id)
                session.state = "guessed"
                await db.commit()
                await manager.broadcast_state(db, session_id, session)

        # --- Host: force advance guessing ---
        elif action == "force_advance_guessing" and is_host and session.state == "guessing":
            manager.cancel_timer(session_id)
            session.state = "guessed"
            await db.commit()
            await manager.broadcast_state(db, session_id, session)

        # --- Host: reveal true author ---
        elif action == "reveal_author" and is_host and session.state == "guessed":
            session.state = "revealed"
            await db.commit()
            await manager.broadcast_state(db, session_id, session)

        # --- Host: next answer ---
        elif action == "next_answer" and is_host and session.state == "revealed":
            order = manager.answer_order.get(session_id, [])
            next_idx = session.current_answer_index + 1
            if next_idx >= len(order):
                session.state = "stats"
                manager.cancel_timer(session_id)
            else:
                session.current_answer_index = next_idx
                session.state = "guessing"
            await db.commit()
            # Start timer BEFORE broadcast_state for next answer
            if session.state == "guessing" and session.guess_timer_seconds > 0:
                manager.start_timer(session_id, session.guess_timer_seconds)
                await manager.broadcast(session_id, {
                    "type": "timer_start",
                    "seconds": session.guess_timer_seconds,
                })
            await manager.broadcast_state(db, session_id, session)

        # --- Host: update question (lobby only) ---
        elif action == "update_question" and is_host and session.state == "lobby":
            q = msg.get("question", "").strip()
            if q:
                session.question = q
                await db.commit()
                await manager.broadcast_state(db, session_id, session)

        # --- Host: update settings (lobby only) ---
        elif action == "update_settings" and is_host and session.state == "lobby":
            if "guess_timer_seconds" in msg:
                session.guess_timer_seconds = max(0, int(msg["guess_timer_seconds"]))
            if "exclude_revealed_from_guessing" in msg:
                session.exclude_revealed_from_guessing = bool(msg["exclude_revealed_from_guessing"])
            await db.commit()
            await manager.broadcast_state(db, session_id, session)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
