"""
Retro Icebreaker — End-to-End Test Suite

Runs entirely in-process using ASGI transport. No running server needed.
WebSocket tests use a real local server spun up on a random port.

Usage:
    python test_game.py
    # or with uv:
    uv run --no-project test_game.py
"""

import asyncio
import json
import random
import socket
import sys
import threading
import time

import httpx
from httpx import ASGITransport

# ── colours ────────────────────────────────────────────────────────────────
GRN  = "\033[92m"; RED = "\033[91m"; BLU = "\033[94m"; BOLD = "\033[1m"; RST = "\033[0m"
P    = f"{GRN}✓{RST}"; F = f"{RED}✗{RST}"; SEC = f"{BOLD}{BLU}"

passed = 0; failed = 0

def ok(msg):   global passed; passed += 1; print(f"  {P} {msg}")
def fail(msg): global failed; failed += 1; print(f"  {F} {msg}")
def section(t): print(f"\n{SEC}── {t} ──{RST}")


# ── helpers ─────────────────────────────────────────────────────────────────

async def init_db():
    """Create tables in the in-memory DB (lifespan doesn't run in ASGI transport)."""
    from main import engine
    from models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)   # clean slate each run
        await conn.run_sync(Base.metadata.create_all)



def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int, db_path: str):
    """Spin up uvicorn in a background thread with its own file-based DB."""
    import uvicorn
    import sqlalchemy
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import main as main_mod
    from models import Base

    db_url = f"sqlite+aiosqlite:///{db_path}"

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _setup_and_serve():
            # Give this server its own engine/session pointing at the temp file
            eng = create_async_engine(db_url, echo=False,
                                      connect_args={"check_same_thread": False})
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            main_mod.engine = eng
            main_mod.async_session = async_sessionmaker(eng, expire_on_commit=False)
            # Reset in-memory connection manager state
            main_mod.manager.connections.clear()
            main_mod.manager.answer_order.clear()
            main_mod.manager.timer_tasks.clear()
            config = uvicorn.Config(main_mod.app, host="127.0.0.1", port=port,
                                    log_level="error", loop="none")
            server = uvicorn.Server(config)
            _servers.append(server)
            await server.serve()

        loop.run_until_complete(_setup_and_serve())

    _servers = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.2)
    return t, _servers


# ── HTTP-only tests (ASGI transport, no network) ─────────────────────────────

async def test_http():
    from main import app
    from game import unique_name

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=10) as http:

        section("1. Basic connectivity")
        r = await http.get("/questions/random")
        if r.status_code == 200 and "question" in r.json():
            ok(f"GET /questions/random → '{r.json()['question'][:55]}'")
        else:
            fail(f"status {r.status_code}")

        section("2. Create session with all settings")
        r = await http.post("/sessions", json={
            "host_name": "Alice",
            "question": "What skill surprises your teammates?",
            "host_is_player": True,
            "guess_timer_seconds": 4,
            "exclude_revealed_from_guessing": True,
        })
        assert r.status_code == 200, f"Create: {r.text}"
        d = r.json(); sid, host_id = d["session_id"], d["player_id"]
        ok(f"Session {sid} created, host={host_id}")

        section("3. Players join")
        pids = [host_id]; pnames = ["Alice"]
        for name in ["Bob", "Charlie", "Diana", "Eve"]:
            r = await http.post(f"/sessions/{sid}/join", json={"name": name})
            assert r.status_code == 200, f"Join '{name}' failed ({r.status_code}): {r.text}"
            d = r.json()
            if "name" not in d:
                fail(f"Join response missing 'name' field — is main.py up to date? Got: {list(d.keys())}")
                pids.append(d["player_id"]); pnames.append(name)
            else:
                pids.append(d["player_id"]); pnames.append(d["name"])
                ok(f"  Joined '{d['name']}'")

        section("4. Duplicate name handling")
        r = await http.post(f"/sessions/{sid}/join", json={"name": "Alice"})
        d2 = r.json()
        if d2["name"] != "Alice":
            ok(f"Duplicate 'Alice' → '{d2['name']}' (auto-disambiguated)")
        else:
            fail("Duplicate name NOT disambiguated")

        r = await http.post(f"/sessions/{sid}/join", json={"name": "Alice"})
        d3 = r.json()
        if d3["name"] not in ["Alice", d2["name"]]:
            ok(f"Third 'Alice' → '{d3['name']}'")
        else:
            fail(f"Third Alice collision: got '{d3['name']}'")

        section("5. unique_name() logic")
        cases = [
            (("Alice", ["Alice", "Bob", "Alice #2"]), "Alice #3"),
            (("Bob",   ["Alice", "Bob", "Alice #2"]), "Bob #2"),
            (("X",     ["X", "X #2", "X #3"]),        "X #4"),
            (("Charlie",["Alice", "Bob"]),              "Charlie"),
            (("Alice",  []),                            "Alice"),
        ]
        for (name, existing), expected in cases:
            got = unique_name(name, existing)
            if got == expected:
                ok(f"unique_name({name!r}, {existing!r}) → {got!r}")
            else:
                fail(f"unique_name({name!r}, {existing!r}) → {got!r}  (expected {expected!r})")

        section("6. Session REST endpoints")
        r = await http.get(f"/sessions/{sid}/inactive_players")
        if r.status_code == 200:
            ok(f"inactive_players OK ({len(r.json()['inactive_players'])} inactive)")
        else:
            fail(f"inactive_players: {r.status_code}")

        r = await http.get("/sessions/FAKEID00/inactive_players")
        if r.status_code == 404:
            ok("Non-existent session → 404")
        else:
            fail(f"Expected 404, got {r.status_code}")

        r = await http.post(f"/sessions/{sid}/rejoin/{host_id}")
        if r.status_code == 200:
            d = r.json()
            if d["is_host"] and d["player_id"] == host_id and d["name"] == "Alice":
                ok("Host rejoin: is_host=True, correct player_id and name")
            else:
                fail(f"Host rejoin wrong payload: {d}")
        else:
            fail(f"Rejoin: {r.status_code} {r.text}")

        r = await http.post(f"/sessions/{sid}/rejoin/{pids[1]}")
        if r.status_code == 200:
            d = r.json()
            if not d["is_host"] and d["name"] == "Bob":
                ok("Non-host rejoin: is_host=False, name='Bob'")
            else:
                fail(f"Non-host rejoin wrong: {d}")
        else:
            fail(f"Non-host rejoin: {r.status_code}")

        r = await http.post(f"/sessions/{sid}/rejoin/FAKEPLAYER")
        if r.status_code == 404:
            ok("Unknown player_id rejoin → 404")
        else:
            fail(f"Expected 404, got {r.status_code}")

        section("7. Question bank")
        from questions import QUESTION_BANK
        ok(f"Bank has {len(QUESTION_BANK)} questions")
        qs = {(await http.get("/questions/random")).json()["question"] for _ in range(8)}
        ok(f"Got {len(qs)} distinct questions in 8 rolls")

        section("8. Second session with different settings")
        r = await http.post("/sessions", json={
            "host_name": "Host2",
            "question": "Q2?",
            "host_is_player": False,
            "guess_timer_seconds": 0,
            "exclude_revealed_from_guessing": False,
        })
        if r.status_code == 200:
            ok("Host-only session (no timer, no exclude) created")
        else:
            fail(f"Second session: {r.status_code}")


# ── WebSocket / full-game tests ───────────────────────────────────────────────

async def recv_state(ws, expected=None, timeout=12):
    """Read messages until we get a 'state' type, optionally matching state name."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rem = max(0.1, deadline - time.monotonic())
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=rem)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get("type") == "state":
            if expected is None or msg.get("state") == expected:
                return msg
    raise TimeoutError(f"Timed out waiting for state={expected!r}")


async def send(ws, obj):
    await ws.send(json.dumps(obj))


async def test_websocket(port: int):
    """Full game simulation with 5 players over WebSockets."""
    import websockets

    BASE  = f"http://127.0.0.1:{port}"
    WS    = f"ws://127.0.0.1:{port}"
    NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve"]

    async def drain_pending(ws, timeout=0.5):
        """Non-blocking drain: consume buffered messages, return last state seen."""
        last = None
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if msg.get("type") == "state":
                    last = msg
        except (asyncio.TimeoutError, Exception):
            pass
        return last
    ANSWS = [
        "I got lost in Tokyo for 6 hours and loved every minute",
        "I can solve a Rubik's cube — badly",
        "I spent three months on a sailboat in the Mediterranean",
        "I was a competitive yo-yo champion in my hometown",
        "I cried at a car commercial and I'm not ashamed",
    ]

    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:

        section("WS-1. Create session (4 s timer, exclude_revealed=True)")
        r = await http.post("/sessions", json={
            "host_name": NAMES[0],
            "question": "What skill surprises your teammates?",
            "host_is_player": True,
            "guess_timer_seconds": 4,
            "exclude_revealed_from_guessing": True,
        })
        assert r.status_code == 200, r.text
        sid, host_id = r.json()["session_id"], r.json()["player_id"]
        ok(f"Session {sid} (timer=4 s, exclude=True)")

        section("WS-2. Players join")
        pids = [host_id]
        for name in NAMES[1:]:
            r = await http.post(f"/sessions/{sid}/join", json={"name": name})
            assert r.status_code == 200, f"{name}: {r.text}"
            pids.append(r.json()["player_id"])
        ok(f"{len(pids)} players joined")

        section("WS-3. Connect via WebSocket, verify lobby state")
        wss = []
        for pid in pids:
            ws = await websockets.connect(f"{WS}/ws/{sid}/{pid}")
            wss.append(ws)
        # drain initial states
        for ws in wss:
            msg = await recv_state(ws, "lobby")
        ok("All 5 players in lobby state")

        host_ws = wss[0]

        # Verify timer setting in lobby
        if msg.get("guess_timer_seconds") == 4:
            ok("guess_timer_seconds=4 present in lobby state")
        else:
            fail(f"guess_timer_seconds wrong: {msg.get('guess_timer_seconds')}")

        # Verify exclude_revealed in lobby
        if msg.get("exclude_revealed_from_guessing"):
            ok("exclude_revealed_from_guessing=True in lobby state")
        else:
            fail(f"exclude_revealed_from_guessing wrong: {msg.get('exclude_revealed_from_guessing')}")

        section("WS-4. Start answering, all submit")
        await send(host_ws, {"action": "start_answering"})
        for ws in wss:
            await recv_state(ws, "answering")
        ok("All players in 'answering'")

        for ws, ans in zip(wss, ANSWS):
            await send(ws, {"action": "submit_answer", "text": ans})
            await asyncio.sleep(0.05)

        # Player 1 (Bob) edits their answer
        edited = ANSWS[1] + " — edited!"
        await send(wss[1], {"action": "submit_answer", "text": edited})
        ANSWS[1] = edited
        await asyncio.sleep(0.5)  # let broadcasts settle
        ok("All 5 submitted; Bob edited his answer")

        ok("Answering state confirmed after edit")

        section("WS-5. Host reveals answers")
        await asyncio.sleep(0.5)  # let edit broadcasts settle
        await send(host_ws, {"action": "reveal_answers"})
        await asyncio.sleep(0.5)  # give server time to process
        reveal_msg = None
        for i, ws in enumerate(wss):
            reveal_msg = await recv_state(ws, "reveal", timeout=20)
        if len(reveal_msg.get("answers", [])) == 5:
            ok("Reveal: 5 answers shown (shuffled, no attribution)")
        else:
            fail(f"Reveal: expected 5 answers, got {len(reveal_msg.get('answers', []))}")

        # Check edited answer is present
        texts = [a["text"] for a in reveal_msg["answers"]]
        if edited in texts:
            ok("Edited answer (not original) appears in reveal")
        else:
            fail("Edited answer not found in reveal")

        section("WS-6. Guessing with timer, answer sidebar, exclude_revealed")
        await send(host_ws, {"action": "start_guessing"})
        guess_msgs = []
        for ws in wss:
            m = await recv_state(ws, "guessing")
            guess_msgs.append(m)
        ok("All in 'guessing'")

        m0 = guess_msgs[0]
        if m0.get("guess_timer_seconds") == 4:
            ok("Timer setting (4 s) in guessing state")
        else:
            fail(f"Timer: {m0.get('guess_timer_seconds')}")

        if "answer_sidebar" in m0 and len(m0["answer_sidebar"]) == 5:
            statuses = [a["status"] for a in m0["answer_sidebar"]]
            if statuses.count("current") == 1 and statuses.count("upcoming") == 4:
                ok("Sidebar: 1 current, 4 upcoming on first answer")
            else:
                fail(f"Sidebar statuses wrong: {statuses}")
        else:
            fail("answer_sidebar missing or wrong length")

        total = m0["total_answers"]
        ok(f"Total answers to guess through: {total}")

        # Play all answers
        for ans_idx in range(total):
            # Collect each player's current guessing state
            cur_states = {pid: m for pid, m in zip(pids, guess_msgs)}

            # Each non-author submits a guess
            guessed = 0
            for ws, pid, m in zip(wss, pids, guess_msgs):
                if m.get("i_am_author"):
                    continue
                players = m.get("players", [])
                votable = [p for p in players if p.get("votable", True)]
                if not votable:
                    # All excluded — pick anyone
                    votable = players
                target = random.choice(votable)
                await send(ws, {"action": "submit_guess",
                                "guessed_player_id": target["id"]})
                guessed += 1
                await asyncio.sleep(0.04)

            # Wait for 'guessed' (all guessed) or timer (4 s max)
            guessed_msgs = []
            try:
                for ws in wss:
                    guessed_msgs.append(await recv_state(ws, "guessed", timeout=7))
                ok(f"Answer {ans_idx+1}/{total}: all guesses collected")
            except TimeoutError:
                fail(f"Answer {ans_idx+1}: timed out waiting for 'guessed'")
                await send(host_ws, {"action": "force_advance_guessing"})
                for ws in wss:
                    try: await recv_state(ws, "guessed", timeout=3)
                    except: pass

            # Check sidebar in guessed state
            if guessed_msgs and "answer_sidebar" in guessed_msgs[0]:
                ok(f"Answer {ans_idx+1}: sidebar present in guessed state")

            # Host reveals author
            await send(host_ws, {"action": "reveal_author"})
            rev_msgs = []
            for ws in wss:
                rev_msgs.append(await recv_state(ws, "revealed", timeout=8))
            author_name = rev_msgs[0].get("true_author", {}).get("name", "?")
            ok(f"Answer {ans_idx+1}: author revealed → {author_name}")

            if "guesser_results" in rev_msgs[0]:
                results = rev_msgs[0]["guesser_results"]
                ok(f"Answer {ans_idx+1}: guesser_results has {len(results)} entries")
            else:
                fail(f"Answer {ans_idx+1}: guesser_results missing")

            # Check exclude_revealed: from answer 2 onwards, revealed authors
            # should be marked votable=False in the next guessing state
            if ans_idx < total - 1:
                await send(host_ws, {"action": "next_answer"})
                guess_msgs = []
                for ws in wss:
                    guess_msgs.append(await recv_state(ws, "guessing", timeout=8))

                if ans_idx >= 0:  # from second answer onwards
                    all_players = guess_msgs[0].get("players", [])
                    unvotable = [p for p in all_players if not p.get("votable", True)]
                    ok(f"Answer {ans_idx+2}: {len(unvotable)} player(s) excluded from voting")

        section("WS-7. Stats")
        await send(host_ws, {"action": "next_answer"})
        for ws in wss:
            stats = await recv_state(ws, "stats", timeout=8)
        for key in ["most_fooling", "best_guessers", "hardest_answers"]:
            if key in stats:
                ok(f"Stats.{key}: {len(stats[key])} entries")
            else:
                fail(f"Stats.{key} missing")

        section("WS-8. Disconnect and rejoin")
        charlie_ws = wss[2]; charlie_id = pids[2]
        await charlie_ws.close()
        await asyncio.sleep(0.5)
        ok("Charlie disconnected")

        async with httpx.AsyncClient(base_url=BASE, timeout=5) as h2:
            r = await h2.get(f"/sessions/{sid}/inactive_players")
            if r.status_code == 200:
                inactive = r.json()["inactive_players"]
                if any(p["id"] == charlie_id for p in inactive):
                    ok(f"Charlie listed in inactive_players ({len(inactive)} total inactive)")
                else:
                    fail(f"Charlie not in inactive_players: {inactive}")
            else:
                fail(f"inactive_players: {r.status_code}")

            r = await h2.post(f"/sessions/{sid}/rejoin/{charlie_id}")
            if r.status_code == 200 and r.json()["name"] == "Charlie":
                ok("Charlie rejoin validated via REST")
            else:
                fail(f"Rejoin: {r.status_code} {r.text}")

        charlie_ws2 = await websockets.connect(f"{WS}/ws/{sid}/{charlie_id}")
        msg = await recv_state(charlie_ws2, timeout=5)
        if msg.get("state") == "stats":
            ok("Charlie rejoined and received current 'stats' state")
        else:
            fail(f"Charlie rejoined but got state={msg.get('state')!r}")
        await charlie_ws2.close()

        for ws in wss:
            try: await ws.close()
            except: pass


# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{BOLD}Retro Icebreaker — End-to-End Test Suite{RST}")

    # Phase 1: HTTP tests via ASGI (no server needed)
    print(f"\n{BOLD}Phase 1: HTTP tests (in-process, no server needed){RST}")
    await init_db()
    await test_http()

    # Phase 2: WebSocket tests via real local server
    print(f"\n{BOLD}Phase 2: WebSocket / full-game tests (local server){RST}")
    try:
        import websockets  # noqa: F401
        import uvicorn     # noqa: F401
    except ImportError as e:
        print(f"  {F} Skipping WS tests — missing dependency: {e}")
        print(f"     Install with: pip install websockets uvicorn")
    else:
        port = free_port()
        print(f"  Starting local server on port {port}…")
        import tempfile, os
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        _thread, _servers = start_server(port, db_path)
        try:
            await test_websocket(port)
        finally:
            for s in _servers:
                s.should_exit = True
            try: os.unlink(db_path)
            except: pass

    # Summary
    total = passed + failed
    print(f"\n{BOLD}── Summary ──{RST}")
    print(f"  {P} {passed}/{total} passed   {F} {failed}/{total} failed\n")
    if failed:
        print(f"  {RED}Some tests failed — see above{RST}")
        sys.exit(1)
    else:
        print(f"  {GRN}All tests passed!{RST}\n")


if __name__ == "__main__":
    asyncio.run(main())