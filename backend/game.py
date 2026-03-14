"""
Game state machine logic. All mutations go through here.
States: lobby → answering → reveal → guessing → guessed → revealed → stats
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import Session, Player, Answer, Guess
import random


async def get_session(db: AsyncSession, session_id: str) -> Session | None:
    result = await db.execute(select(Session).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def get_all_players(db: AsyncSession, session_id: str) -> list[Player]:
    """All players including inactive (for rejoin lookup)."""
    result = await db.execute(select(Player).where(Player.session_id == session_id))
    return result.scalars().all()


async def get_active_players(db: AsyncSession, session_id: str) -> list[Player]:
    result = await db.execute(
        select(Player).where(Player.session_id == session_id, Player.is_active == True)
    )
    return result.scalars().all()


async def get_answers(db: AsyncSession, session_id: str) -> list[Answer]:
    result = await db.execute(select(Answer).where(Answer.session_id == session_id))
    return result.scalars().all()


async def get_guesses_for_answer(db: AsyncSession, answer_id: str) -> list[Guess]:
    result = await db.execute(select(Guess).where(Guess.answer_id == answer_id))
    return result.scalars().all()


def unique_name(desired: str, existing_names: list[str]) -> str:
    """If desired name is taken, append #2, #3, etc."""
    if desired not in existing_names:
        return desired
    i = 2
    while f"{desired} #{i}" in existing_names:
        i += 1
    return f"{desired} #{i}"


async def build_lobby_state(db: AsyncSession, session: Session) -> dict:
    players = await get_active_players(db, session.id)
    return {
        "type": "state",
        "state": "lobby",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "host_is_player": session.host_is_player,
        "guess_timer_seconds": session.guess_timer_seconds,
        "exclude_revealed_from_guessing": session.exclude_revealed_from_guessing,
        "players": [{"id": p.id, "name": p.name, "is_host": p.is_host} for p in players],
    }


async def build_answering_state(db: AsyncSession, session: Session, viewer_id: str) -> dict:
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    submitted_ids = {a.player_id for a in answers}
    # Send the viewer their own current answer text so they can edit it
    my_answer = next((a.text for a in answers if a.player_id == viewer_id), None)
    return {
        "type": "state",
        "state": "answering",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "players": [{"id": p.id, "name": p.name, "submitted": p.id in submitted_ids} for p in players],
        "i_submitted": viewer_id in submitted_ids,
        "my_answer": my_answer,
    }


async def build_reveal_state(db: AsyncSession, session: Session) -> dict:
    """All answers revealed, shuffled, no attribution."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    shuffled = list(answers)
    random.shuffle(shuffled)
    return {
        "type": "state",
        "state": "reveal",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "players": [{"id": p.id, "name": p.name} for p in players],
        "answers": [{"id": a.id, "text": a.text} for a in shuffled],
    }


async def build_guessing_state(
    db: AsyncSession,
    session: Session,
    viewer_id: str,
    answer_order: list[str],
) -> dict:
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

    # Eligible guessers: everyone except the author
    eligible_guessers = [p for p in players if p.id != current_answer.player_id]

    i_am_author = viewer_id == current_answer.player_id
    i_guessed = viewer_id in guessed_by

    # Build the answer sidebar: past (greyed), current (green), upcoming (normal)
    past_answer_ids = set(answer_order[:idx])
    answer_sidebar = []
    for i, aid in enumerate(answer_order):
        a = answer_map.get(aid)
        if not a:
            continue
        if i < idx:
            status = "past"
        elif i == idx:
            status = "current"
        else:
            status = "upcoming"
        answer_sidebar.append({"text": a.text, "status": status})

    # Players that can be voted on — depends on exclude_revealed_from_guessing setting
    # Authors of already-revealed answers are excluded if the flag is set
    revealed_author_ids: set[str] = set()
    if session.exclude_revealed_from_guessing:
        for past_aid in past_answer_ids:
            past_answer = answer_map.get(past_aid)
            if past_answer:
                revealed_author_ids.add(past_answer.player_id)

    # Build player list with votable flag
    player_list = []
    for p in players:
        votable = True
        if session.exclude_revealed_from_guessing and p.id in revealed_author_ids:
            votable = False
        player_list.append({"id": p.id, "name": p.name, "votable": votable})

    return {
        "type": "state",
        "state": "guessing",
        "session_id": session.id,
        "question": session.question,
        "host_id": session.host_id,
        "answer_index": idx,
        "total_answers": len(answer_order),
        "current_answer": {"id": current_answer.id, "text": current_answer.text},
        "players": player_list,
        "eligible_guessers": [p.id for p in eligible_guessers],
        "guessed_so_far": list(guessed_by),
        "i_am_author": i_am_author,
        "i_guessed": i_guessed,
        "answer_sidebar": answer_sidebar,
        "guess_timer_seconds": session.guess_timer_seconds,
        "exclude_revealed_from_guessing": session.exclude_revealed_from_guessing,
    }


async def build_guessed_state(
    db: AsyncSession,
    session: Session,
    answer_id: str,
    answer_order: list[str],
) -> dict:
    """Show guess distribution (counts only) before author reveal."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    answer_map = {a.id: a for a in answers}
    player_map = {p.id: p for p in players}

    current_answer = answer_map.get(answer_id)
    guesses = await get_guesses_for_answer(db, answer_id)

    distribution = {}
    for g in guesses:
        gp = player_map.get(g.guessed_player_id)
        if gp:
            distribution.setdefault(g.guessed_player_id, {"name": gp.name, "count": 0})
            distribution[g.guessed_player_id]["count"] += 1

    idx = session.current_answer_index
    past_answer_ids = set(answer_order[:idx])
    answer_sidebar = []
    for i, aid in enumerate(answer_order):
        a = answer_map.get(aid)
        if not a:
            continue
        status = "past" if i < idx else ("current" if i == idx else "upcoming")
        answer_sidebar.append({"text": a.text, "status": status})

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
        "answer_sidebar": answer_sidebar,
    }


async def build_revealed_state(
    db: AsyncSession,
    session: Session,
    answer_id: str,
    answer_order: list[str],
) -> dict:
    """True author revealed — now safe to show full guesser breakdown."""
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    answer_map = {a.id: a for a in answers}
    player_map = {p.id: p for p in players}

    current_answer = answer_map.get(answer_id)
    guesses = await get_guesses_for_answer(db, answer_id)
    author = player_map.get(current_answer.player_id)

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

    guesser_results = {g.guesser_id: g.is_correct for g in guesses}

    idx = session.current_answer_index
    answer_sidebar = []
    for i, aid in enumerate(answer_order):
        a = answer_map.get(aid)
        if not a:
            continue
        # In revealed state, current answer is now also "past" visually
        status = "past" if i <= idx else "upcoming"
        answer_sidebar.append({"text": a.text, "status": status})

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
        "answer_sidebar": answer_sidebar,
    }


async def build_stats_state(db: AsyncSession, session: Session) -> dict:
    players = await get_active_players(db, session.id)
    answers = await get_answers(db, session.id)
    player_map = {p.id: p for p in players}

    fooled_counts = {p.id: 0 for p in players}
    correct_counts = {p.id: 0 for p in players}
    answer_correct_rates = []

    for answer in answers:
        guesses = await get_guesses_for_answer(db, answer.id)
        correct = sum(1 for g in guesses if g.is_correct)
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