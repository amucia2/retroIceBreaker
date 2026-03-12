"""
Game state machine logic. All mutations go through here.
States: lobby → answering → reveal → guessing → guessed → stats
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import Session, Player, Answer, Guess
import random


async def get_session(db: AsyncSession, session_id: str) -> Session | None:
    result = await db.execute(select(Session).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def get_active_players(db: AsyncSession, session_id: str) -> list[Player]:
    result = await db.execute(
        select(Player).where(Player.session_id == session_id, Player.is_active == True)
    )
    return result.scalars().all()


async def get_answers(db: AsyncSession, session_id: str) -> list[Answer]:
    result = await db.execute(
        select(Answer).where(Answer.session_id == session_id)
    )
    return result.scalars().all()


async def get_guesses_for_answer(db: AsyncSession, answer_id: str) -> list[Guess]:
    result = await db.execute(
        select(Guess).where(Guess.answer_id == answer_id)
    )
    return result.scalars().all()


async def build_lobby_state(db: AsyncSession, session: Session) -> dict:
    players = await get_active_players(db, session.id)
    return {
        "type": "state",
        "state": "lobby",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "host_is_player": session.host_is_player,
        "players": [{"id": p.id, "name": p.name, "is_host": p.is_host} for p in players],
    }


async def build_answering_state(db: AsyncSession, session: Session, viewer_id: str) -> dict:
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    submitted_ids = {a.player_id for a in answers}
    return {
        "type": "state",
        "state": "answering",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "players": [{"id": p.id, "name": p.name, "submitted": p.id in submitted_ids} for p in players],
        "i_submitted": viewer_id in submitted_ids,
    }


async def build_reveal_state(db: AsyncSession, session: Session) -> dict:
    """All answers revealed, shuffled, no attribution."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    shuffled = list(answers)
    random.shuffle(shuffled)
    # Store shuffle order in session for guessing phase consistency
    # We persist order via answer index mapped in memory — store it in session.state metadata
    return {
        "type": "state",
        "state": "reveal",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "players": [{"id": p.id, "name": p.name} for p in players],
        "answers": [{"id": a.id, "text": a.text} for a in shuffled],
    }


async def build_guessing_state(db: AsyncSession, session: Session, viewer_id: str, answer_order: list[str]) -> dict:
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    answer_map = {a.id: a for a in answers}

    idx = session.current_answer_index
    if idx >= len(answer_order):
        return await build_stats_state(db, session)

    current_answer_id = answer_order[idx]
    current_answer = answer_map.get(current_answer_id)
    if not current_answer:
        return {"type": "error", "message": "Answer not found"}

    guesses = await get_guesses_for_answer(db, current_answer_id)
    guessed_by = {g.guesser_id for g in guesses}

    # Eligible guessers = active players who are NOT the author
    eligible_guessers = [
        p for p in players
        if p.id != current_answer.player_id
    ]

    i_am_author = viewer_id == current_answer.player_id
    i_guessed = viewer_id in guessed_by

    return {
        "type": "state",
        "state": "guessing",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "answer_index": idx,
        "total_answers": len(answer_order),
        "current_answer": {"id": current_answer.id, "text": current_answer.text},
        # All players shown as guess targets — author included — so absence isn't a tell
        "players": [{"id": p.id, "name": p.name} for p in players],
        "eligible_guessers": [p.id for p in eligible_guessers],
        "guessed_so_far": list(guessed_by),
        "i_am_author": i_am_author,
        "i_guessed": i_guessed,
    }


async def build_guessed_state(db: AsyncSession, session: Session, answer_id: str, answer_order: list[str]) -> dict:
    """Show guess distribution before author reveal."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    answer_map = {a.id: a for a in answers}
    player_map = {p.id: p for p in players}

    current_answer = answer_map.get(answer_id)
    guesses = await get_guesses_for_answer(db, answer_id)

    # Build distribution: guessed_player_id → vote count only (no guesser names — would leak author identity)
    distribution = {}
    for g in guesses:
        gp = player_map.get(g.guessed_player_id)
        if gp:
            distribution.setdefault(g.guessed_player_id, {"name": gp.name, "count": 0})
            distribution[g.guessed_player_id]["count"] += 1

    idx = session.current_answer_index
    return {
        "type": "state",
        "state": "guessed",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "answer_index": idx,
        "total_answers": len(answer_order),
        "current_answer": {"id": current_answer.id, "text": current_answer.text},
        "players": [{"id": p.id, "name": p.name} for p in players],
        "guess_distribution": list(distribution.values()),
    }


async def build_revealed_state(db: AsyncSession, session: Session, answer_id: str, answer_order: list[str]) -> dict:
    """True author revealed — now safe to show full guesser breakdown."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    answer_map = {a.id: a for a in answers}
    player_map = {p.id: p for p in players}

    current_answer = answer_map.get(answer_id)
    guesses = await get_guesses_for_answer(db, answer_id)
    author = player_map.get(current_answer.player_id)

    # Full distribution with guesser names + correct flag — author is now revealed so no deduction risk
    distribution = {}
    for g in guesses:
        gp = player_map.get(g.guessed_player_id)
        gr = player_map.get(g.guesser_id)
        if gp and gr:
            distribution.setdefault(g.guessed_player_id, {"name": gp.name, "count": 0, "guessers": []})
            distribution[g.guessed_player_id]["count"] += 1
            distribution[g.guessed_player_id]["guessers"].append({
                "name": gr.name,
                "id": gr.id,
                "correct": g.is_correct,
            })

    # Flat lookup: guesser_id → is_correct, so the frontend can personalise the viewer's own result
    guesser_results = {g.guesser_id: g.is_correct for g in guesses}

    idx = session.current_answer_index
    return {
        "type": "state",
        "state": "revealed",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "answer_index": idx,
        "total_answers": len(answer_order),
        "current_answer": {"id": current_answer.id, "text": current_answer.text},
        "players": [{"id": p.id, "name": p.name} for p in players],
        "guess_distribution": list(distribution.values()),
        "true_author": {"id": author.id, "name": author.name} if author else None,
        "guesser_results": guesser_results,
    }


async def build_stats_state(db: AsyncSession, session: Session) -> dict:
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    player_map = {p.id: p for p in players}

    # Aggregate stats
    fooled_counts = {p.id: 0 for p in players}       # how many wrong guesses landed on this person
    correct_counts = {p.id: 0 for p in players}       # how many correct guesses this person made
    answer_correct_rates = []                          # for "hardest to identify"

    for answer in answers:
        guesses = await get_guesses_for_answer(db, answer.id)
        correct = sum(1 for g in guesses if g.is_correct)
        wrong = sum(1 for g in guesses if not g.is_correct)
        total = len(guesses)

        for g in guesses:
            if g.is_correct:
                correct_counts[g.guesser_id] = correct_counts.get(g.guesser_id, 0) + 1
            else:
                fooled_counts[g.guessed_player_id] = fooled_counts.get(g.guessed_player_id, 0) + 1

        author = player_map.get(answer.player_id)
        answer_correct_rates.append({
            "text": answer.text,
            "author": author.name if author else "?",
            "correct_guesses": correct,
            "total_guesses": total,
            "pct_correct": round(correct / total * 100) if total else 0,
        })

    hardest = sorted(answer_correct_rates, key=lambda x: x["pct_correct"])[:3]

    return {
        "type": "state",
        "state": "stats",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "players": [{"id": p.id, "name": p.name} for p in players],
        "most_fooling": sorted(
            [{"name": player_map[pid].name, "count": c} for pid, c in fooled_counts.items() if pid in player_map],
            key=lambda x: -x["count"]
        )[:3],
        "best_guessers": sorted(
            [{"name": player_map[pid].name, "count": c} for pid, c in correct_counts.items() if pid in player_map],
            key=lambda x: -x["count"]
        )[:3],
        "hardest_answers": hardest,
    }