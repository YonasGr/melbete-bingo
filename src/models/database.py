"""
Database Models for Melbete Bingo
"""
from sqlalchemy import Column, String, Integer, Boolean, DateTime, JSON, ForeignKey, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

Base = declarative_base()


def generate_uuid():
    return str(uuid.uuid4())


class Player(Base):
    """Player model"""
    __tablename__ = "players"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    telegram_id = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    cards = relationship("Card", back_populates="owner")
    rooms_created = relationship("GameRoom", back_populates="host")


class GameRoom(Base):
    """Game room/session model"""
    __tablename__ = "game_rooms"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    host_id = Column(String, ForeignKey("players.id"), nullable=False)
    variant = Column(String, nullable=False, default="75")  # 75-ball or 90-ball
    number_range_min = Column(Integer, nullable=False, default=1)
    number_range_max = Column(Integer, nullable=False, default=75)
    cards_per_player = Column(Integer, nullable=False, default=1)
    pattern = Column(JSON, nullable=False)  # Pattern configuration
    state = Column(String, nullable=False, default="lobby")  # lobby, running, verifying, finished
    called_numbers = Column(JSON, nullable=False, default=list)
    draw_pool = Column(JSON, nullable=False, default=list)
    winners = Column(JSON, nullable=False, default=list)
    draw_interval = Column(Integer, nullable=False, default=5)  # seconds
    auto_draw = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    host = relationship("Player", back_populates="rooms_created")
    cards = relationship("Card", back_populates="room")
    draw_logs = relationship("DrawLog", back_populates="room")


class Card(Base):
    """Bingo card model"""
    __tablename__ = "cards"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    room_id = Column(String, ForeignKey("game_rooms.id"), nullable=False)
    owner_id = Column(String, ForeignKey("players.id"), nullable=False)
    variant = Column(String, nullable=False)
    grid = Column(JSON, nullable=False)  # 2D array of cells
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    owner = relationship("Player", back_populates="cards")
    room = relationship("GameRoom", back_populates="cards")


class DrawLog(Base):
    """Draw audit log model"""
    __tablename__ = "draw_logs"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    room_id = Column(String, ForeignKey("game_rooms.id"), nullable=False)
    sequence = Column(JSON, nullable=False)  # List of draw records
    seed = Column(String, nullable=False)
    final_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    room = relationship("GameRoom", back_populates="draw_logs")


class Claim(Base):
    """Bingo claim model"""
    __tablename__ = "claims"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    room_id = Column(String, ForeignKey("game_rooms.id"), nullable=False)
    player_id = Column(String, ForeignKey("players.id"), nullable=False)
    card_id = Column(String, ForeignKey("cards.id"), nullable=False)
    claimed_pattern = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending, accepted, rejected
    verification_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)
