"""
SQLAlchemy 2.0 ORM models — Phase 1 schema.

Tables built now:   schools, quizzes, questions, quiz_questions
Tables deferred:    users (Phase 2), topics (Phase 3), results/answers (Phase 6)

Column names follow the ROADMAP spec (text / options / correct_index).
The app layer translates between the legacy API names (q / opts / correct)
and these DB names inside save_quiz() and load_quiz() only.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class School(Base):
    """
    Added in Phase 1 so the FK column exists before Phase 2 needs it.
    No multi-tenancy logic is wired yet — school_id on quizzes and questions
    is nullable, and Phase 1 inserts never set it.
    Phase 2 will backfill and add NOT NULL via a migration.
    """
    __tablename__ = "schools"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name       = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    quizzes   = relationship("Quiz",     back_populates="school")
    questions = relationship("Question", back_populates="school")


class Quiz(Base):
    __tablename__ = "quizzes"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id       = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=True)
    title           = Column(String(500), nullable=False)
    source_filename = Column(String(500), nullable=False, server_default="")
    created_at      = Column(DateTime(timezone=True), nullable=False, default=_now)

    # created_by deferred to Phase 2 (requires users table)

    school         = relationship("School", back_populates="quizzes")
    quiz_questions = relationship(
        "QuizQuestion",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.position",
    )


class Question(Base):
    __tablename__ = "questions"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id     = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=True)
    text          = Column(Text, nullable=False)
    options       = Column(ARRAY(Text), nullable=False)  # ["Stack", "Queue", ...]
    correct_index = Column(Integer, nullable=False)       # 0-based index into options

    # topic_id and difficulty deferred to Phase 3

    school         = relationship("School", back_populates="questions")
    quiz_questions = relationship("QuizQuestion", back_populates="question")


class QuizQuestion(Base):
    """
    Join table linking quizzes to questions.

    `position` preserves the order questions were added to a quiz.
    Without it, SELECT order is undefined and the frontend would render
    questions in random order. It also enables Phase 3's question bank:
    the same Question row can be linked to many quizzes at different positions.
    """
    __tablename__ = "quiz_questions"

    quiz_id     = Column(UUID(as_uuid=False), ForeignKey("quizzes.id",    ondelete="CASCADE"), primary_key=True)
    question_id = Column(UUID(as_uuid=False), ForeignKey("questions.id",  ondelete="CASCADE"), primary_key=True)
    position    = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("quiz_id", "position", name="uq_quiz_question_position"),
    )

    quiz     = relationship("Quiz",     back_populates="quiz_questions")
    question = relationship("Question", back_populates="quiz_questions")
