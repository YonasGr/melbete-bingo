"""Postgres (Supabase) repository layer.

Each repository encapsulates all reads/writes for one table, so the rest
of the app (API routes, bot handlers) never talks to the database
directly.
"""
from .admin_repository import AdminRepository
from .agent_repository import AgentRepository
from .engagement_repository import EngagementRepository
from .notification_repository import NotificationRepository
from .bot_repository import BotRepository
from .card_repository import CardRepository
from .claim_repository import ClaimRepository
from .draw_log_repository import DrawLogRepository
from .link_code_repository import LinkCodeRepository
from .payment_method_repository import PaymentMethodRepository
from .player_repository import PlayerRepository
from .referral_repository import ReferralRepository
from .room_repository import RoomRepository
from .settings_repository import SettingsRepository
from .transaction_repository import TransactionRepository
from .wallet_repository import WalletRepository

__all__ = [
    "PlayerRepository",
    "RoomRepository",
    "CardRepository",
    "ClaimRepository",
    "DrawLogRepository",
    "WalletRepository",
    "TransactionRepository",
    "AdminRepository",
    "AgentRepository",
    "EngagementRepository",
    "NotificationRepository",
    "PaymentMethodRepository",
    "SettingsRepository",
    "ReferralRepository",
    "LinkCodeRepository",
    "BotRepository",
]
