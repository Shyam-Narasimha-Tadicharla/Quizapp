"""
SQLAlchemy 2.0 ORM models — Phase 4 schema.

Tables built now:   schools, quizzes, questions, quiz_questions, users,
                    subjects, subject_topics, user_subjects
Tables deferred:    results/answers (Phase 6)

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
    __tablename__ = "schools"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name       = Column(String(255), nullable=False)
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


class Question(Base):
    __tablename__ = "questions"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    school_id     = Column(UUID(as_uuid=False), ForeignKey("schools.id"), nullable=False)
    text          = Column(Text, nullable=False)
    options       = Column(ARRAY(Text), nullable=False)
    correct_index = Column(Integer, nullable=False)
    topic         = Column(String(255), nullable=True)   # e.g. "Algebra", "World War II"

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
