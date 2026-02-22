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
import time
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

# Duels older than this (seconds) can be cancelled automatically or manually.
DUEL_EXPIRY_SECONDS: int = int(os.environ.get("DUEL_EXPIRY_SECONDS", "600"))

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
    created_at  REAL    NOT NULL DEFAULT 0,
    FOREIGN KEY (creator_id)  REFERENCES users(telegram_id),
    FOREIGN KEY (acceptor_id) REFERENCES users(telegram_id),
    FOREIGN KEY (winner_id)   REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS house_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    duel_id    INTEGER NOT NULL,
    fee_amount REAL    NOT NULL,
    created_at REAL    NOT NULL DEFAULT 0,
    FOREIGN KEY (duel_id) REFERENCES duels(id)
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
            "INSERT INTO duels (creator_id, amount, status, created_at) "
            "VALUES (?, ?, 'PENDING', ?)",
            (creator_id, amount, time.time()),
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


async def cancel_duel(duel_id: int) -> None:
    """Mark a duel as CANCELLED."""
    async with db_connect() as db:
        await db.execute(
            "UPDATE duels SET status = 'CANCELLED' WHERE id = ?", (duel_id,)
        )
        await db.commit()


async def get_user_pending_duels(telegram_id: int) -> list[dict]:
    """Return all PENDING duels created by a user."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM duels WHERE creator_id = ? AND status = 'PENDING' "
            "ORDER BY id DESC",
            (telegram_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def expire_stale_duels() -> list[dict]:
    """Find and cancel duels that have exceeded the expiry window.
    Returns the list of expired duels so the caller can refund creators."""
    cutoff = time.time() - DUEL_EXPIRY_SECONDS
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT * FROM duels WHERE status = 'PENDING' AND created_at < ?",
            (cutoff,),
        )
        stale = [dict(r) for r in await cur.fetchall()]
        if stale:
            await db.execute(
                "UPDATE duels SET status = 'EXPIRED' "
                "WHERE status = 'PENDING' AND created_at < ?",
                (cutoff,),
            )
            await db.commit()
        return stale


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


async def record_house_fee(duel_id: int, fee_amount: float) -> None:
    """Log the house fee for a finished duel into the house_ledger table."""
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO house_ledger (duel_id, fee_amount, created_at) "
            "VALUES (?, ?, ?)",
            (duel_id, fee_amount, time.time()),
        )
        await db.commit()


async def get_total_house_fees() -> float:
    """Return the sum of all house fees collected."""
    async with db_connect() as db:
        cur = await db.execute("SELECT COALESCE(SUM(fee_amount), 0) FROM house_ledger")
        row = await cur.fetchone()
        return float(row[0])


async def process_duel(duel: dict, acceptor_id: int) -> tuple[int, float, float]:
    """
    Execute a duel: pick the winner, distribute funds, update the DB.

    Returns (winner_id, winner_payout, house_fee).

    Rake calculation (3 % of total pot):
        total_pot     = amount * 2
        house_fee     = total_pot * HOUSE_FEE
        winner_payout = total_pot - house_fee
    """
    creator_id: int = duel["creator_id"]
    amount: float = duel["amount"]

    # Deduct the bet from the acceptor
    await adjust_balance(acceptor_id, -amount)

    # Determine winner (fair 50/50)
    winner_id = random.choice([creator_id, acceptor_id])

    # --- Rake / fee calculation ---
    total_pot: float = amount * 2             # both sides combined
    house_fee: float = total_pot * HOUSE_FEE  # 3 % of pot
    winner_payout: float = total_pot - house_fee  # 97 % of pot

    # Credit the winner
    await adjust_balance(winner_id, winner_payout)

    # Mark the duel as finished
    await finish_duel(duel["id"], acceptor_id, winner_id)

    # Record the house fee in the ledger
    await record_house_fee(duel["id"], house_fee)

    return winner_id, winner_payout, house_fee


# ---------------------------------------------------------------------------
# History / leaderboard queries
# ---------------------------------------------------------------------------


async def get_recent_duels(limit: int = 10) -> list[dict]:
    """Return the most recent finished duels."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT d.*, "
            "  u1.username AS creator_name, "
            "  u2.username AS acceptor_name, "
            "  u3.username AS winner_name "
            "FROM duels d "
            "LEFT JOIN users u1 ON u1.telegram_id = d.creator_id "
            "LEFT JOIN users u2 ON u2.telegram_id = d.acceptor_id "
            "LEFT JOIN users u3 ON u3.telegram_id = d.winner_id "
            "WHERE d.status = 'FINISHED' "
            "ORDER BY d.id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_leaderboard(limit: int = 10) -> list[dict]:
    """Return top users ranked by total winnings from duels."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT u.username, u.telegram_id, u.balance, "
            "  COUNT(d.id) AS wins, "
            "  COALESCE(SUM(d.amount * 2 * (1 - ?)), 0) AS total_won "
            "FROM users u "
            "LEFT JOIN duels d ON d.winner_id = u.telegram_id "
            "  AND d.status = 'FINISHED' "
            "GROUP BY u.telegram_id "
            "ORDER BY wins DESC, total_won DESC "
            "LIMIT ?",
            (HOUSE_FEE, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_user_stats(telegram_id: int) -> dict:
    """Return win/loss stats for a single user."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT "
            "  COUNT(CASE WHEN winner_id = ? THEN 1 END) AS wins, "
            "  COUNT(CASE WHEN (creator_id = ? OR acceptor_id = ?) "
            "    AND winner_id != ? AND status = 'FINISHED' THEN 1 END) AS losses "
            "FROM duels "
            "WHERE (creator_id = ? OR acceptor_id = ?) AND status = 'FINISHED'",
            (telegram_id, telegram_id, telegram_id,
             telegram_id, telegram_id, telegram_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else {"wins": 0, "losses": 0}


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
        "Welcome to <b>DuelBot</b>! You've been registered.\n\n"
        "<b>Commands:</b>\n"
        "/deposit [amount] — Add credits (default 1000)\n"
        "/balance — Check your balance\n"
        "/duel &lt;amount&gt; — Challenge someone to a coin flip\n"
        "/cancel — Cancel your pending duel\n"
        "/history — View recent duels\n"
        "/stats — Your win/loss record\n"
        "/leaderboard — Top players",
        parse_mode="HTML",
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
        f"Deposited <b>{amount:,.2f}</b> credits.\n"
        f"New balance: <b>{new_balance:,.2f}</b>",
        parse_mode="HTML",
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/balance — show the user's current balance."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)
    bal = await get_balance(user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Your balance: <b>{bal:,.2f}</b> credits",
        parse_mode="HTML",
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

    # Check for existing pending duel by this user
    existing = await get_user_pending_duels(user.id)
    if existing:
        await update.message.reply_text(  # type: ignore[union-attr]
            "You already have a pending duel. Use /cancel first."
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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — cancel your pending duel and get a refund."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    pending = await get_user_pending_duels(user.id)
    if not pending:
        await update.message.reply_text(  # type: ignore[union-attr]
            "You have no pending duels to cancel."
        )
        return

    refund_total = 0.0
    for duel in pending:
        await cancel_duel(duel["id"])
        await adjust_balance(user.id, duel["amount"])
        refund_total += duel["amount"]

    new_balance = await get_balance(user.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Cancelled {len(pending)} duel(s). "
        f"Refunded <b>{refund_total:,.2f}</b> credits.\n"
        f"Balance: <b>{new_balance:,.2f}</b>",
        parse_mode="HTML",
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history — show the 10 most recent completed duels."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    duels = await get_recent_duels(limit=10)
    if not duels:
        await update.message.reply_text(  # type: ignore[union-attr]
            "No duels have been played yet."
        )
        return

    lines: list[str] = ["<b>Recent Duels</b>\n"]
    for d in duels:
        pot = d["amount"] * 2
        creator = d["creator_name"] or str(d["creator_id"])
        acceptor = d["acceptor_name"] or str(d["acceptor_id"])
        winner = d["winner_name"] or str(d["winner_id"])
        lines.append(
            f"#{d['id']}  {creator} vs {acceptor}  "
            f"| Pot ${pot:,.2f} | Winner: <b>{winner}</b>"
        )

    await update.message.reply_text(  # type: ignore[union-attr]
        "\n".join(lines), parse_mode="HTML"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — show your win/loss record."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    stats = await get_user_stats(user.id)
    bal = await get_balance(user.id)
    wins = stats["wins"]
    losses = stats["losses"]
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    await update.message.reply_text(  # type: ignore[union-attr]
        f"<b>Your Stats</b>\n"
        f"Balance: {bal:,.2f}\n"
        f"Duels played: {total}\n"
        f"Wins: {wins}  |  Losses: {losses}\n"
        f"Win rate: {win_rate:.1f}%",
        parse_mode="HTML",
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/leaderboard — show top 10 players by wins."""
    user = update.effective_user
    if not user:
        return
    await ensure_user(user.id, user.username or user.first_name)

    board = await get_leaderboard(limit=10)
    if not board:
        await update.message.reply_text(  # type: ignore[union-attr]
            "No players yet."
        )
        return

    lines: list[str] = ["<b>Leaderboard</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(board):
        prefix = medals[i] if i < 3 else f"  {i + 1}."
        name = row["username"] or str(row["telegram_id"])
        lines.append(
            f"{prefix} <b>{name}</b> — "
            f"{row['wins']} win(s) | "
            f"Balance: {row['balance']:,.2f}"
        )

    await update.message.reply_text(  # type: ignore[union-attr]
        "\n".join(lines), parse_mode="HTML"
    )


async def cmd_house(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/house — show total fees collected by the house (admin only info, public)."""
    user = update.effective_user
    if not user:
        return
    total = await get_total_house_fees()
    await update.message.reply_text(  # type: ignore[union-attr]
        f"🏦 <b>House Wallet</b>\nTotal fees collected: <b>${total:,.2f}</b>",
        parse_mode="HTML",
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
# Scheduled job: expire stale duels
# ---------------------------------------------------------------------------


async def job_expire_duels(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job that expires old pending duels and refunds creators."""
    stale = await expire_stale_duels()
    for duel in stale:
        await adjust_balance(duel["creator_id"], duel["amount"])
        logger.info(
            "Expired duel #%d — refunded %.2f to user %d",
            duel["id"], duel["amount"], duel["creator_id"],
        )


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    """Run once after the application is initialized (before polling)."""
    await init_db()
    logger.info("Database initialized.")

    # Schedule periodic duel-expiry check every 60 seconds
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            job_expire_duels,
            interval=60,
            first=10,
            name="expire_duels",
        )
        logger.info("Duel expiry job scheduled (every 60s).")


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

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("duel", cmd_duel))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("house", cmd_house))

    # Register callback handler for inline buttons
    app.add_handler(
        CallbackQueryHandler(callback_accept_duel, pattern=r"^accept_duel:\d+$")
    )

    logger.info("DuelBot is starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
