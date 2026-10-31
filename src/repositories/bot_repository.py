"""Postgres repository for bot_players table."""
from typing import List, Optional
from src.models.schemas import BotPlayer, Player, Wallet, new_id
from src.repositories.base import BaseRepository
from src.repositories.player_repository import PlayerRepository
from src.repositories.wallet_repository import WalletRepository

ETHIOPIAN_NAMES = [
    "Abatu_G", "Abdi_Tafa", "AbdulhakimJ", "Mejid_A", "Abel_S",
    "Abeni_Y", "Abu_Kaba", "Addis_S", "Adugna_J", "Aman_Bedru",
    "Amanuel_M", "Amanuel_Y", "Amar_A", "Amir_M", "Amria_G",
    "Anwar_Y", "Arebu_M", "Arefat_B", "Asladin_A", "Atro_S",
    "Barudin_U", "Beka_B", "Beka_G", "Beneyam_Y", "Bilal_D",
    "Bilal_T", "Bitaniya_M", "Chala_F", "Dawit_T", "Debisa_Y",
    "Dereje_K", "Diriba_G", "Eden_N", "Eleni_M", "Eleni_Z",
    "Eyasu_A", "Eyerusalem_K", "Eyuel_C", "Ezana_Z", "Fenet_B",
    "Feyisa_G", "Feysal_H", "Filmon_E", "Firesenbet", "Gosa_T",
    "Hasset_S", "Hermon_N", "Hussen_A", "Ismail_A", "Jalane_A",
    "Jemal_S", "Jiregna_D", "Kalkidan_B", "Karma_T", "Kirubel_G",
    "Mahlet_T", "Megarsa_G", "Meron_D", "Mesud_H", "Miftah_F",
    "Mihiret_E", "Muhajer_B", "Mulachew_A", "Murad_D", "Nahom_E",
    "Natnael_B", "Paulos_T", "Paulos_Ti", "Pulos_T", "Rashad_A",
    "Remedan_M", "Roza_M", "Saron_S", "Sebisibe_R", "Singitan_A",
    "Sofonias_S", "Tajudin_A", "Tekalign_G", "Teshome_M", "Tsion_T",
    "Tumsa_R", "Usman_A", "Wada_N", "Wasihun_T", "Yohannes_K",
    "Yonas_GG", "Yonas_Gz", "Ziyad_S",
]


class BotRepository(BaseRepository):
    table_name = "bot_players"

    async def list_bots(self, active_only: bool = True) -> List[BotPlayer]:
        query = "SELECT * FROM bot_players"
        if active_only:
            query += " WHERE is_active = TRUE"
        query += " ORDER BY created_at ASC"
        rows = await self._fetch(query)
        return [BotPlayer(**r) for r in rows]

    async def get_by_player_id(self, player_id: str) -> Optional[BotPlayer]:
        row = await self._fetchrow("SELECT * FROM bot_players WHERE player_id = $1", player_id)
        return BotPlayer(**row) if row else None

    async def list_bots_for_rotation(self, active_only: bool = True) -> List[BotPlayer]:
        """Bots ordered least-recently-played first (never-played bots
        come first of all). Picking bots off the front of this list is
        how the bot service rotates through the whole pool of 50 instead
        of the same handful tending to get picked every round."""
        query = "SELECT * FROM bot_players"
        if active_only:
            query += " WHERE is_active = TRUE"
        query += " ORDER BY last_played_at ASC NULLS FIRST, created_at ASC"
        rows = await self._fetch(query)
        return [BotPlayer(**r) for r in rows]

    async def mark_played(self, player_ids: List[str]) -> None:
        """Stamp last_played_at = now() for the given bots, right after
        they join a room, so the next round's rotation picks different
        bots first."""
        if not player_ids:
            return
        pool = await self._pool()
        await pool.execute(
            "UPDATE bot_players SET last_played_at = now() WHERE player_id = ANY($1::text[])",
            player_ids,
        )

    async def ensure_bots_seeded(self, count: int = 50) -> List[BotPlayer]:
        """Ensure `count` bot players are registered in bot_players, players, and pre-funded in wallets."""
        existing = await self.list_bots(active_only=False)
        if len(existing) >= count:
            return existing[:count]

        player_repo = PlayerRepository()
        wallet_repo = WalletRepository()

        bots: List[BotPlayer] = list(existing)
        needed = count - len(existing)

        for i in range(needed):
            idx = len(bots) + 1
            player_id = f"bot_player_{idx}"
            display_name = ETHIOPIAN_NAMES[(idx - 1) % len(ETHIOPIAN_NAMES)]

            # 1. Ensure Player record
            existing_p = await player_repo.get_by_id(player_id)
            if not existing_p:
                p = Player(
                    id=player_id,
                    telegram_id=f"bot_tg_{idx}",
                    display_name=display_name,
                    registration_state="complete",
                    is_bot=True,
                )
                await player_repo.create(p)

            # 2. Ensure Wallet record with 50,000 ETB in both real and
            # bonus balance, so a bot can fund a stake in any of the 3
            # lobby tracks (real_10, real_50, bonus). None of this is
            # actual user money either way - it's simulated house
            # liquidity so bots can play as genuine, fairly-random
            # opponents in every room.
            w = await wallet_repo.get_by_player(player_id)
            if not w:
                w = Wallet(
                    player_id=player_id,
                    balance=50000,
                    won_balance=50000,
                    bonus_balance=50000,
                )
                await wallet_repo.create(w)
            elif w.balance < 1000:
                await wallet_repo.update(player_id, {"balance": 50000, "won_balance": 50000, "bonus_balance": 50000})

            # 3. Ensure bot_players record
            b_row = await self._fetchrow("SELECT * FROM bot_players WHERE player_id = $1", player_id)
            if not b_row:
                bot_rec = BotPlayer(
                    player_id=player_id,
                    display_name=display_name,
                    is_active=True,
                )
                await self._execute(
                    "INSERT INTO bot_players (id, player_id, display_name, is_active) VALUES ($1, $2, $3, $4)",
                    bot_rec.id, bot_rec.player_id, bot_rec.display_name, bot_rec.is_active
                )
                bots.append(bot_rec)
            else:
                bots.append(BotPlayer(**b_row))

        return bots
