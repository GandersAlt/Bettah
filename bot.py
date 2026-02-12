"""
DuelBot (MVP) — PvP Coin-Flip Telegram Bot
============================================
A text-based coin-flip duel bot for Telegram group chats.
Users can deposit credits, challenge each other to coin flips,
and the winner takes the pot minus a 3 % house fee.

Tech: python-telegram-bot v20+, aiosqlite, Python 3.10+
"""

from __future__ import annotations

import logging
import os
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
DB_PATH: str = os.environ.get("DB_PATH", "duelbot.db")

# House fee percentage applied to the total pot.
#
# Rake calculation:
#   total_pot  = bet_amount * 2          (both players wager equally)
#   house_fee  = total_pot * HOUSE_FEE   (3 % of the pot)
#   winner_pay = total_pot - house_fee   (winner receives 97 % of the pot)
#
# Example with a $50 duel:
#   total_pot  = 50 * 2       = 100
#   house_fee  = 100 * 0.03   = 3
#   winner_pay = 100 - 3      = 97
HOUSE_FEE: float = 0.03

DEFAULT_DEPOSIT: float = 1000.0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username    TEXT    NOT NULL DEFAULT '',
    balance     REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS duels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id  INTEGER NOT NULL,
    acceptor_id INTEGER,
    amount      REAL    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'PENDING',
    winner_id   INTEGER,
    FOREIGN KEY (creator_id)  REFERENCES users(telegram_id),
    FOREIGN KEY (acceptor_id) REFERENCES users(telegram_id),
    FOREIGN KEY (winner_id)   REFERENCES users(telegram_id)
);
"""


@asynccontextmanager
async def db_connect() -> AsyncIterator[aiosqlite.Connection]:
    """Yield an aiosqlite connection with WAL mode and foreign keys enabled."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create tables if they don't exist yet."""
    async with db_connect() as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------


async def ensure_user(telegram_id: int, username: str) -> None:
    """Insert or update a user row (upsert)."""
    async with db_connect() as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, balance)
            VALUES (?, ?, 0.0)
            ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
            """,
            (telegram_id, username),
        )
        await db.commit()


async def get_balance(telegram_id: int) -> float:
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return float(row["balance"]) if row else 0.0


async def adjust_balance(telegram_id: int, delta: float) -> None:
    """Add *delta* to a user's balance (can be negative)."""
    async with db_connect() as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
            (delta, telegram_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Duel helpers
# ---------------------------------------------------------------------------


async def create_duel(creator_id: int, amount: float) -> int:
    """Create a PENDING duel and return its row id."""
    async with db_connect() as db:
        cur = await db.execute(
            "INSERT INTO duels (creator_id, amount, status) VALUES (?, ?, 'PENDING')",
            (creator_id, amount),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def get_pending_duel(duel_id: int) -> dict | None:
    """Return the duel row if it exists and is still PENDING."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM duels WHERE id = ? AND status = 'PENDING'", (duel_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def finish_duel(
    duel_id: int, acceptor_id: int, winner_id: int
) -> None:
    """Mark a duel as FINISHED and record the acceptor + winner."""
    async with db_connect() as db:
        await db.execute(
            """
            UPDATE duels
               SET acceptor_id = ?, winner_id = ?, status = 'FINISHED'
             WHERE id = ?
            """,
            (acceptor_id, winner_id, duel_id),
        )
        await db.commit()


async def process_duel(duel: dict, acceptor_id: int) -> tuple[int, float, float]:
    """
    Execute a duel: pick the winner, distribute funds, update the DB.

    Returns (winner_id, winner_payout, house_fee).

    Rake calculation (3 % of total pot):
        total_pot    = amount * 2
        house_fee    = total_pot * HOUSE_FEE
        winner_payout = total_pot - house_fee
    """
    creator_id: int = duel["creator_id"]
    amount: float = duel["amount"]

    # Deduct the bet from the acceptor
    await adjust_balance(acceptor_id, -amount)

    # Determine winner (fair 50/50)
    winner_id = random.choice([creator_id, acceptor_id])
    loser_id = acceptor_id if winner_id == creator_id else creator_id  # noqa: F841

    # --- Rake / fee calculation ---
    total_pot: float = amount * 2           # both sides combined
    house_fee: float = total_pot * HOUSE_FEE  # 3 % of pot
    winner_payout: float = total_pot - house_fee  # 97 % of pot

    # Credit the winner
    await adjust_balance(winner_id, winner_payout)

    # Mark the duel as finished
    await finish_duel(duel["id"], acceptor_id, winner_id)

    return winner_id, winner_payout, house_fee


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — register the user and show a welcome message."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Welcome, {user.first_name}! You've been registered.\n"
        "Use /deposit to add credits, /balance to check your balance, "
        "and /duel <amount> to challenge someone!"
    )


async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deposit [amount] — simulate a deposit (default 1000 credits)."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    amount = DEFAULT_DEPOSIT
    if context.args:
        try:
            amount = float(context.args[0])
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Usage: /deposit [amount]  (positive number)"
            )
            return

    await adjust_balance(user.id, amount)
    new_balance = await get_balance(user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Deposited {amount:,.2f} credits.\nNew balance: {new_balance:,.2f}"
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/balance — show the user's current balance."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)
    bal = await get_balance(user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Your balance: {bal:,.2f} credits"
    )


async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/duel <amount> — create a pending coin-flip duel."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    if not context.args:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Usage: /duel <amount>"
        )
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(  # type: ignore[union-attr]
            "Amount must be a positive number."
        )
        return

    balance = await get_balance(user.id)
    if balance < amount:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"Insufficient funds. Your balance: {balance:,.2f}"
        )
        return

    # Deduct bet from the creator immediately
    await adjust_balance(user.id, -amount)

    duel_id = await create_duel(user.id, amount)
    display_name = user.username or user.first_name

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Accept Duel (${amount:,.2f})",
                    callback_data=f"accept_duel:{duel_id}",
                )
            ]
        ]
    )

    await update.message.reply_text(  # type: ignore[union-attr]
        f"⚔️ <b>{display_name}</b> wants to flip for <b>${amount:,.2f}</b>!\n"
        "<i>Waiting for a challenger...</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Callback query handler (button press)
# ---------------------------------------------------------------------------


async def callback_accept_duel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the 'Accept Duel' inline button press."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    acceptor = update.effective_user
    if not acceptor:
        return
    await ensure_user(acceptor.id, acceptor.username or acceptor.first_name)

    # Parse callback data
    try:
        _, duel_id_str = query.data.split(":")
        duel_id = int(duel_id_str)
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid duel data.")
        return

    duel = await get_pending_duel(duel_id)
    if duel is None:
        await query.edit_message_text("This duel is no longer available.")
        return

    creator_id: int = duel["creator_id"]
    amount: float = duel["amount"]

    # Validation: cannot duel yourself
    if acceptor.id == creator_id:
        await query.answer("You cannot duel yourself!", show_alert=True)
        return

    # Validation: sufficient balance
    acceptor_balance = await get_balance(acceptor.id)
    if acceptor_balance < amount:
        await query.answer(
            f"Insufficient funds. Your balance: {acceptor_balance:,.2f}",
            show_alert=True,
        )
        return

    # --- Execute the duel ---
    winner_id, winner_payout, house_fee = await process_duel(duel, acceptor.id)

    # Resolve display names
    acceptor_name = acceptor.username or acceptor.first_name
    try:
        creator_member = await context.bot.get_chat_member(
            query.message.chat_id, creator_id  # type: ignore[union-attr]
        )
        creator_name = (
            creator_member.user.username or creator_member.user.first_name
        )
    except Exception:
        creator_name = str(creator_id)

    winner_name = creator_name if winner_id == creator_id else acceptor_name
    loser_name = acceptor_name if winner_id == creator_id else creator_name

    # Edit the original message to show the result
    await query.edit_message_text(
        f"⚔️ <b>Duel Complete!</b>\n\n"
        f"🪙 <b>{creator_name}</b> vs <b>{acceptor_name}</b>\n"
        f"💰 Pot: ${amount * 2:,.2f}\n\n"
        f"🏆 Winner: <b>{winner_name}</b> (+${winner_payout:,.2f})\n"
        f"😢 Loser: {loser_name}\n"
        f"🏦 House fee (3%): ${house_fee:,.2f}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    """Run once after the application is initialized (before polling)."""
    await init_db()
    logger.info("Database initialized.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Export it before running: export BOT_TOKEN='your-token-here'"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("duel", cmd_duel))
    app.add_handler(
        CallbackQueryHandler(callback_accept_duel, pattern=r"^accept_duel:\d+$")
    )

    logger.info("DuelBot is starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
