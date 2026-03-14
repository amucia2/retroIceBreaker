from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime
import uuid


class Base(DeclarativeBase):
    pass


def new_id():
    return str(uuid.uuid4())[:8].upper()


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True, default=new_id)
    question = Column(Text, nullable=False)
    host_id = Column(String, nullable=False)
    host_is_player = Column(Boolean, default=True)
    state = Column(String, default="lobby")
    # States: lobby → answering → reveal → guessing → guessed → revealed → stats
    current_answer_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Guessing timer in seconds (0 = no timer, host advances manually)
    guess_timer_seconds = Column(Integer, default=30)

    # If True, players whose answers have already been revealed cannot be voted on
    exclude_revealed_from_guessing = Column(Boolean, default=False)

    players = relationship("Player", back_populates="session", cascade="all, delete-orphan")
    answers = relationship("Answer", back_populates="session", cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id = Column(String, primary_key=True, default=new_id)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    name = Column(String, nullable=False)
    is_host = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    session = relationship("Session", back_populates="players")
    answers = relationship("Answer", back_populates="player", cascade="all, delete-orphan")
    guesses = relationship("Guess", back_populates="guesser", foreign_keys="Guess.guesser_id", cascade="all, delete-orphan")


class Answer(Base):
    __tablename__ = "answers"

    id = Column(String, primary_key=True, default=new_id)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    player_id = Column(String, ForeignKey("players.id"), nullable=False)
    text = Column(Text, nullable=False)
    submitted_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("Session", back_populates="answers")
    player = relationship("Player", back_populates="answers")
    guesses = relationship("Guess", back_populates="answer", cascade="all, delete-orphan")


class Guess(Base):
    __tablename__ = "guesses"

    id = Column(String, primary_key=True, default=new_id)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    answer_id = Column(String, ForeignKey("answers.id"), nullable=False)
    guesser_id = Column(String, ForeignKey("players.id"), nullable=False)
    guessed_player_id = Column(String, ForeignKey("players.id"), nullable=False)
    is_correct = Column(Boolean, default=False)

    answer = relationship("Answer", back_populates="guesses")
    guesser = relationship("Player", back_populates="guesses", foreign_keys=[guesser_id])
    guessed_player = relationship("Player", foreign_keys=[guessed_player_id])