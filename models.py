"""
SQLAlchemy 2.0 ORM models — Phase 4 schema.

Tables: schools, quizzes, questions, quiz_questions, users,
        subjects, subject_topics, user_subjects,
        assignments, results, answers, result_topic_scores
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class School(Base):
    __tablename__ = "schools"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name       = Column(String(255), nullable=False)
    domain     = Column(String(253), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    quizzes   = relationship("Quiz",     back_populates="school")
    questions = relationship("Question", back_populates="school")
    users     = relationship("User",     back_populates="school")
    subjects  = relationship("Subject",  back_populates="school")


class User(Base):
    """
    Maps a Supabase Auth user (auth_id = their sub claim UUID) to a school.
    role is either 'admin' or 'teacher'. One user belongs to exactly one school.
    """
    __tablename__ = "users"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    auth_id    = Column(UUID(as_uuid=False), nullable=False, unique=True)  # Supabase auth.users.id
    email      = Column(String(255), nullable=True)                        # stored for display; added Phase 4
    school_id  = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=False)
    role       = Column(String(20), nullable=False, default="teacher")     # 'admin' | 'teacher'
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    school         = relationship("School",      back_populates="users")
    subject_links  = relationship("UserSubject", back_populates="user", cascade="all, delete-orphan")


class Quiz(Base):
    __tablename__ = "quizzes"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id       = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=False)
    created_by      = Column(UUID(as_uuid=False), ForeignKey("users.id"),   nullable=False)
    title           = Column(String(500), nullable=False)
    source_filename = Column(String(500), nullable=False, server_default="")
    created_at      = Column(DateTime(timezone=True), nullable=False, default=_now)

    school         = relationship("School", back_populates="quizzes")
    quiz_questions = relationship(
        "QuizQuestion",
        back_populates="quiz",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.position",
    )
    assignments    = relationship("Assignment", back_populates="quiz")


class Question(Base):
    __tablename__ = "questions"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id     = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=False)
    text          = Column(Text, nullable=False)
    options       = Column(ARRAY(Text), nullable=False)
    correct_index = Column(Integer, nullable=False)
    topic         = Column(String(255), nullable=True)
    deleted_at    = Column(DateTime(timezone=True), nullable=True)  # soft delete

    school         = relationship("School", back_populates="questions")
    quiz_questions = relationship("QuizQuestion", back_populates="question")
    answers        = relationship("Answer", back_populates="question")


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


# ── Phase 4: subjects ─────────────────────────────────────────────────────────

class SubjectTopic(Base):
    """Links a free-form topic string to a subject group."""
    __tablename__ = "subject_topics"

    subject_id = Column(UUID(as_uuid=False), ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True)
    topic      = Column(String(255), primary_key=True)

    subject = relationship("Subject", back_populates="topic_links")


class UserSubject(Base):
    """Assigns a subject to a user (teacher or admin)."""
    __tablename__ = "user_subjects"

    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id",    ondelete="CASCADE"), primary_key=True)
    subject_id = Column(UUID(as_uuid=False), ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True)

    user    = relationship("User",    back_populates="subject_links")
    subject = relationship("Subject", back_populates="user_links")


class Subject(Base):
    """A named subject grouping (e.g. 'Mathematics') scoped to a school."""
    __tablename__ = "subjects"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id  = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=False)
    name       = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("school_id", "name", name="uq_subject_school_name"),
    )

    school      = relationship("School",       back_populates="subjects")
    topic_links = relationship("SubjectTopic", back_populates="subject", cascade="all, delete-orphan")
    user_links  = relationship("UserSubject",  back_populates="subject", cascade="all, delete-orphan")


# ── Phase 2/3 completion: assignments, results, answers ───────────────────────

class Assignment(Base):
    """A teacher assigns a quiz to a class via a class_name code."""
    __tablename__ = "assignments"

    id                   = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id            = Column(UUID(as_uuid=False), ForeignKey("schools.id"),              nullable=False)
    quiz_id              = Column(UUID(as_uuid=False), ForeignKey("quizzes.id",  ondelete="CASCADE"), nullable=True)
    created_by           = Column(UUID(as_uuid=False), ForeignKey("users.id"),                nullable=False)
    class_name           = Column(String(100), nullable=False)
    opens_at             = Column(DateTime(timezone=True), nullable=True)
    closes_at            = Column(DateTime(timezone=True), nullable=True)
    created_at           = Column(DateTime(timezone=True), nullable=False, default=_now)
    # Phase 4 randomization
    mode                 = Column(String(20),  nullable=False, default="manual")   # 'manual'|'randomized'|'total_random'
    randomize_questions  = Column(Boolean,     nullable=False, default=False)
    randomize_options    = Column(Boolean,     nullable=False, default=False)
    topic_rules          = Column(JSONB,       nullable=True)  # [{topic, count}] for modes 2 & 3
    # Phase 5 timed delivery
    duration_minutes     = Column(Integer,     nullable=True)  # None = no timer

    quiz    = relationship("Quiz",   back_populates="assignments")
    results = relationship("Result", back_populates="assignment", cascade="all, delete-orphan")


class Result(Base):
    """One student's attempt at an assigned quiz."""
    __tablename__ = "results"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    assignment_id  = Column(UUID(as_uuid=False), ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    student_name   = Column(String(255), nullable=False)
    roll_number    = Column(String(50),  nullable=False)
    class_name     = Column(String(100), nullable=False)
    score          = Column(Integer, nullable=False)
    total          = Column(Integer, nullable=False)
    submitted_at   = Column(DateTime(timezone=True), nullable=False, default=_now)
    # Phase 4 shuffle maps — stored so scoring and review are always correct
    question_order = Column(JSONB, nullable=True)  # [question_id, ...]  in seen order
    option_orders  = Column(JSONB, nullable=True)  # {question_id: [original_idx, ...]}
    # Phase 5 timed delivery
    started_at     = Column(DateTime(timezone=True), nullable=True)   # server-side start time
    is_complete    = Column(Boolean, nullable=False, default=False)    # False until submit succeeds

    assignment   = relationship("Assignment",       back_populates="results")
    answers      = relationship("Answer",           back_populates="result", cascade="all, delete-orphan")
    topic_scores = relationship("ResultTopicScore", back_populates="result", cascade="all, delete-orphan")


class Answer(Base):
    """One answer row per question per student attempt."""
    __tablename__ = "answers"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    result_id    = Column(UUID(as_uuid=False), ForeignKey("results.id",   ondelete="CASCADE"), nullable=False)
    question_id  = Column(UUID(as_uuid=False), ForeignKey("questions.id", ondelete="SET NULL"), nullable=True)
    chosen_index = Column(Integer, nullable=False)
    is_correct   = Column(Boolean, nullable=False)

    result   = relationship("Result",   back_populates="answers")
    question = relationship("Question", back_populates="answers")


class ResultTopicScore(Base):
    """Per-topic breakdown for a single student attempt — used for analytics."""
    __tablename__ = "result_topic_scores"

    id        = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    result_id = Column(UUID(as_uuid=False), ForeignKey("results.id", ondelete="CASCADE"), nullable=False)
    topic     = Column(String(255), nullable=False)
    correct   = Column(Integer,     nullable=False)
    total     = Column(Integer,     nullable=False)

    result = relationship("Result", back_populates="topic_scores")
