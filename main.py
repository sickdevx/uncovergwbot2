import asyncio
import html
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================== CONFIG ====================
BOT_TOKEN = "8851065867:AAH7yOhYgJ2P6u_CJ00o6CcnAulj5az2i18"  # Set this in your environment
ADMIN_IDS = [5226135306]  # Your Telegram ID(s)

REFRESH_SECONDS = 30          # how often live posts are refreshed
MAX_TITLE_LENGTH = 200
MAX_DRAW_ATTEMPTS = 50        # max entrants re-checked for channel membership at draw time
DATA_FILE = Path(__file__).with_name("giveaways.json")
CHANNEL_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== STATE ====================
# giveaways[id] = {
#   "id", "title", "hosted_by", "end_time" (ISO, UTC), "channels": [...],
#   "photo": file_id | None, "status": "active" | "ended" | "cancelled",
#   "participants": {user_id: {"name", "username"}},
#   "messages": [[chat_id, message_id], ...],
#   "winner": {"id", "name", "username"} | None,
#   "preset_winner": {"id", "name", "username"} | None,
#   "final_entries": int   (set when the giveaway finishes)
# }
giveaways: dict[str, dict] = {}
_last_refresh: dict[str, float] = {}
_last_text: dict[str, str] = {}

NO_KEYBOARD = InlineKeyboardMarkup([])

USAGE = (
    "🎁 <b>Create a giveaway</b>\n\n"
    "<code>/giveaway 2h Win a free month of Netflix | @channel</code>\n"
    "<code>/giveaway 1d12h $50 Gift Card | @ch1 @ch2 winner:123456789</code>\n\n"
    "• <b>Duration</b>: 30 (minutes), 30m, 5h, 2d, or combined like 1d12h\n"
    "• <b>Title</b>: everything after the duration\n"
    "• <b>Channels</b> (optional): after a <code>|</code>. "
    "The giveaway is posted only in these channels. I must be an admin there, "
    "and entrants must be members to join.\n"
    "• <b>Preset winner</b> (optional): add <code>winner:USER_ID</code> or "
    "<code>winner:@username</code> after the channels.\n"
    "• <b>Picture</b> (optional): send a photo with the command as its caption, "
    "or reply to a photo with the command.\n\n"
    "Without a preset winner the winner is picked at random from everyone who entered."
)

HELP_ADMIN = (
    "🎉 <b>Giveaway Bot</b>\n\n"
    "<b>Commands</b>\n"
    "/giveaway — host a new giveaway\n"
    "/giveaways — list active giveaways\n"
    "/endgiveaway &lt;id&gt; — draw the winner now\n"
    "/cancelgiveaway &lt;id&gt; — cancel a giveaway\n\n"
    + USAGE
)

HELP_USER = (
    "🎉 <b>Giveaway Bot</b>\n\n"
    "I run giveaways! Watch for a post with a <b>Join Giveaway</b> button and tap it to enter."
)


# ==================== HELPERS ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_state() -> None:
    if not DATA_FILE.exists():
        return
    try:
        giveaways.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        logger.info("Loaded %d giveaway(s) from %s", len(giveaways), DATA_FILE.name)
    except Exception:
        backup = DATA_FILE.with_suffix(".corrupt")
        logger.exception("Could not read %s, moving it to %s", DATA_FILE.name, backup.name)
        os.replace(DATA_FILE, backup)


def save_state() -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(giveaways, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def new_giveaway_id() -> str:
    while True:
        gid = str(random.randint(10000, 99999))
        if gid not in giveaways:
            return gid


def parse_duration(text: str) -> timedelta | None:
    """'30' -> 30 min, '30m', '5h', '2d', or combined like '1d12h30m'."""
    text = text.lower().strip()
    if text.isdigit():
        total = timedelta(minutes=int(text)) if len(text) < 7 else None
    elif re.fullmatch(r"(?:\d{1,6}[dhm])+", text):
        units = {"d": "days", "h": "hours", "m": "minutes"}
        total = timedelta()
        for value, unit in re.findall(r"(\d+)([dhm])", text):
            total += timedelta(**{units[unit]: int(value)})
    else:
        return None
    if total is None or total.total_seconds() <= 0 or total > timedelta(days=365):
        return None
    return total


def parse_command(text: str) -> tuple[str, str, list[str], str | None]:
    """'/giveaway 2h Title | @a @b winner:123' -> duration, title, channels, winner_spec"""
    parts = text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    left, _, right = rest.partition("|")
    head = left.strip().split(maxsplit=1)
    duration_str = head[0] if head else ""
    title = head[1].strip() if len(head) > 1 else ""

    tokens = right.split()
    winner_spec = None
    channels = []
    for tok in tokens:
        if tok.lower().startswith("winner:"):
            winner_spec = tok.split(":", 1)[1].strip()
        else:
            channels.append(tok)
    return duration_str, title, channels, winner_spec


def format_time_left(end_time: datetime) -> str:
    seconds = int((end_time - now_utc()).total_seconds())
    if seconds <= 0:
        return "Ending now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


def entry_count(gw: dict) -> int:
    return gw["final_entries"] if "final_entries" in gw else len(gw["participants"])


def user_link(info: dict) -> str:
    name = html.escape(info.get("name") or "Winner")
    link = f'<a href="tg://user?id={info["id"]}">{name}</a>'
    if info.get("username"):
        link += f" (@{html.escape(info['username'])})"
    return link


def join_keyboard(gid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎉 Join Giveaway", callback_data=f"join_{gid}")]]
    )


def render_post(gw: dict) -> str:
    lines = [
        f"🎁 <b>{html.escape(gw['title'])}</b>",
        "",
        f"👤 Hosted by: {html.escape(gw['hosted_by'])}",
        f"👥 Entries: {entry_count(gw)}",
    ]
    status = gw["status"]
    if status == "active":
        lines.append(f"⏳ Ends in: <b>{format_time_left(parse_iso(gw['end_time']))}</b>")
        if gw["channels"]:
            lines += ["", "📋 <b>To enter, you must be a member of:</b>"]
            lines += [f"• {html.escape(ch)}" for ch in gw["channels"]]
        lines += ["", "Tap the button below to join! 👇"]
    elif status == "ended":
        lines.append("")
        if gw.get("winner"):
            lines += [f"🏆 Winner: {user_link(gw['winner'])}", "", "🎊 Congratulations!"]
        else:
            lines.append("😕 No eligible winner this time.")
        lines += ["", "🔒 This giveaway has ended."]
    else:
        lines += ["", "❌ This giveaway was cancelled."]
    return "\n".join(lines)


# ==================== TELEGRAM HELPERS ====================
async def check_user_in_channels(bot, user_id: int, channels: list) -> tuple[bool, list]:
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            is_member = member.status in ("creator", "administrator", "member") or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
            if not is_member:
                missing.append(channel)
        except Exception as e:
            logger.warning("Could not check %s for user %s: %s", channel, user_id, e)
            missing.append(channel)
    return len(missing) == 0, missing


async def send_post(bot, chat_id, gw: dict, text: str, keyboard):
    if gw.get("photo"):
        return await bot.send_photo(
            chat_id=chat_id, photo=gw["photo"], caption=text,
            parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    return await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
    )


async def edit_post(bot, chat_id, message_id: int, gw: dict, text: str, keyboard) -> None:
    for _ in range(2):  # one retry if Telegram asks us to slow down
        try:
            if gw.get("photo"):
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=text,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            return
        except RetryAfter as e:
            delay = e.retry_after
            delay = delay.total_seconds() if isinstance(delay, timedelta) else delay
            await asyncio.sleep(min(float(delay), 30) + 1)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning("Could not edit post in %s: %s", chat_id, e)
            return
        except Exception as e:
            logger.warning("Could not edit post in %s: %s", chat_id, e)
            return


async def refresh_messages(
    context: ContextTypes.DEFAULT_TYPE, gid: str, min_interval: float = 0.0, force: bool = False
) -> None:
    """Re-render every post of a giveaway. Skips the edit if nothing changed."""
    gw = giveaways.get(gid)
    if not gw:
        return
    now = time.monotonic()
    if now - _last_refresh.get(gid, 0.0) < min_interval:
        return  # too soon; the periodic refresh will catch up
    text = render_post(gw)
    if not force and _last_text.get(gid) == text:
        return
    _last_refresh[gid] = now
    _last_text[gid] = text
    keyboard = join_keyboard(gid) if gw["status"] == "active" else NO_KEYBOARD
    for chat_id, message_id in gw["messages"]:
        await edit_post(context.bot, chat_id, message_id, gw, text, keyboard)


# ==================== JOBS ====================
async def refresh_job(context: ContextTypes.DEFAULT_TYPE):
    await refresh_messages(context, context.job.data)


async def end_job(context: ContextTypes.DEFAULT_TYPE):
    await finish_giveaway(context, context.job.data)


def schedule_giveaway(job_queue, gid: str, end_time: datetime) -> None:
    remaining = max((end_time - now_utc()).total_seconds(), 1)
    job_queue.run_repeating(
        refresh_job, interval=REFRESH_SECONDS, first=REFRESH_SECONDS,
        data=gid, name=f"update_{gid}",
    )
    job_queue.run_once(end_job, when=remaining, data=gid, name=f"end_{gid}")


def drop_jobs(job_queue, gid: str) -> None:
    for prefix in ("update", "end"):
        for job in job_queue.get_jobs_by_name(f"{prefix}_{gid}"):
            job.schedule_removal()


# ==================== DRAWING ====================
async def pick_winner(bot, gw: dict) -> dict | None:
    """Random pick among entrants who still meet the channel requirements."""
    candidates = list(gw["participants"].items())
    random.SystemRandom().shuffle(candidates)
    for uid, info in candidates[:MAX_DRAW_ATTEMPTS]:
        if gw["channels"]:
            ok, _ = await check_user_in_channels(bot, int(uid), gw["channels"])
            if not ok:
                continue
        return {"id": int(uid), **info}
    return None


async def finish_giveaway(context: ContextTypes.DEFAULT_TYPE, gid: str) -> None:
    gw = giveaways.get(gid)
    if not gw or gw["status"] != "active":
        return

    gw["status"] = "ended"  # set first so nothing can join or end it twice
    drop_jobs(context.job_queue, gid)

    # Use preset winner if one was set, otherwise pick randomly
    if gw.get("preset_winner"):
        gw["winner"] = gw["preset_winner"]
    else:
        gw["winner"] = await pick_winner(context.bot, gw)

    gw["final_entries"] = len(gw["participants"])
    gw["participants"] = {}  # no need to keep entrant data around
    save_state()

    # Update the original giveaway post(s) with the final result
    await refresh_messages(context, gid, force=True)


# ==================== COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_ADMIN if is_admin(update.effective_user.id) else HELP_USER
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def giveaway_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not is_admin(user.id):
        await msg.reply_text("❌ Only admins can host giveaways.")
        return

    duration_str, title, channels, winner_spec = parse_command(msg.text or msg.caption or "")
    duration = parse_duration(duration_str)
    if duration is None:
        await msg.reply_text(USAGE, parse_mode=ParseMode.HTML)
        return

    # Optional photo: attached to this message, or the one being replied to
    photo_file_id = None
    replied = msg.reply_to_message
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
    elif replied and replied.photo:
        photo_file_id = replied.photo[-1].file_id
        if not title and replied.caption:
            title = replied.caption.strip()

    title = title or "Giveaway"
    if len(title) > MAX_TITLE_LENGTH:
        await msg.reply_text(f"❌ Title is too long ({len(title)}/{MAX_TITLE_LENGTH} characters).")
        return

    # Validate channels before posting anything
    channels = list(dict.fromkeys(channels))
    bad_format = [c for c in channels if not CHANNEL_RE.fullmatch(c)]
    if bad_format:
        await msg.reply_text(
            "❌ Channels must look like @channelname. Problem with: " + ", ".join(bad_format)
        )
        return
    no_access = []
    for ch in channels:
        try:
            me = await context.bot.get_chat_member(ch, context.bot.id)
            if me.status not in ("administrator", "creator"):
                no_access.append(ch)
        except Exception:
            no_access.append(ch)
    if no_access:
        await msg.reply_text(
            "❌ I can't use these channels: " + ", ".join(no_access)
            + "\nMake sure they exist and that I'm an admin there, then try again."
        )
        return

    # Resolve optional preset winner
    preset_winner = None
    if winner_spec:
        try:
            if winner_spec.isdigit():
                chat = await context.bot.get_chat(int(winner_spec))
            else:
                uname = winner_spec if winner_spec.startswith("@") else f"@{winner_spec}"
                chat = await context.bot.get_chat(uname)
            preset_winner = {
                "id": chat.id,
                "name": chat.full_name or getattr(chat, "title", None) or "Winner",
                "username": chat.username,
            }
        except Exception as e:
            logger.warning("Could not resolve winner %s: %s", winner_spec, e)
            await msg.reply_text(
                f"❌ Could not resolve winner '{html.escape(winner_spec)}'. "
                "Use a numeric user ID or a public @username the bot can see."
            )
            return

    gid = new_giveaway_id()
    end_time = now_utc() + duration
    gw = {
        "id": gid,
        "title": title,
        "hosted_by": f"@{user.username}" if user.username else user.full_name,
        "end_time": end_time.isoformat(),
        "channels": channels,
        "photo": photo_file_id,
        "status": "active",
        "participants": {},
        "messages": [],
        "winner": None,
        "preset_winner": preset_winner,
    }
    giveaways[gid] = gw

    text = render_post(gw)
    keyboard = join_keyboard(gid)

    # Post only to the required channels when they are given
    if channels:
        targets = channels
    else:
        targets = [update.effective_chat.id]

    failed = []
    for target in targets:
        try:
            sent = await send_post(context.bot, target, gw, text, keyboard)
            gw["messages"].append([target, sent.message_id])
        except Exception as e:
            logger.warning("Could not post in %s: %s", target, e)
            failed.append(target)

    if not gw["messages"]:
        del giveaways[gid]
        await msg.reply_text("❌ I couldn't post the giveaway anywhere. Check my permissions and try again.")
        return

    save_state()
    schedule_giveaway(context.job_queue, gid, end_time)

    reply = (
        "✅ <b>Giveaway started!</b>\n\n"
        f"🎁 {html.escape(title)}\n"
        f"🆔 ID: <code>{gid}</code>\n"
        f"⏳ Duration: {html.escape(duration_str)}\n"
        f"📍 Posted in {len(gw['messages'])} place(s)\n"
    )
    if preset_winner:
        reply += f"🎯 Preset winner: {user_link(preset_winner)}\n"
    reply += (
        f"\nDraw now: /endgiveaway {gid}\n"
        f"Cancel: /cancelgiveaway {gid}"
    )
    if failed:
        reply += "\n\n⚠️ Could not post in: " + html.escape(", ".join(map(str, failed)))
    await msg.reply_text(reply, parse_mode=ParseMode.HTML)


async def list_giveaways(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("❌ Only admins can do that.")
        return
    active = [gw for gw in giveaways.values() if gw["status"] == "active"]
    if not active:
        await msg.reply_text("No active giveaways. Start one with /giveaway")
        return
    lines = ["📋 <b>Active giveaways</b>", ""]
    for gw in active:
        lines.append(
            f"🆔 <code>{gw['id']}</code> — {html.escape(gw['title'])}\n"
            f"     👥 {entry_count(gw)} entries · ⏳ {format_time_left(parse_iso(gw['end_time']))}"
        )
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def end_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("❌ Only admins can do that.")
        return
    if not context.args:
        await msg.reply_text("Usage: /endgiveaway <id>")
        return
    gid = context.args[0]
    gw = giveaways.get(gid)
    if not gw:
        await msg.reply_text("Giveaway not found.")
        return
    if gw["status"] != "active":
        await msg.reply_text("That giveaway has already finished.")
        return
    await msg.reply_text("🎲 Drawing the winner now…")
    await finish_giveaway(context, gid)


async def cancel_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_admin(update.effective_user.id):
        await msg.reply_text("❌ Only admins can cancel.")
        return
    if not context.args:
        await msg.reply_text("Usage: /cancelgiveaway <id>")
        return
    gid = context.args[0]
    gw = giveaways.get(gid)
    if not gw:
        await msg.reply_text("Giveaway not found.")
        return
    if gw["status"] != "active":
        await msg.reply_text("That giveaway has already finished.")
        return

    gw["status"] = "cancelled"
    gw["final_entries"] = len(gw["participants"])
    gw["participants"] = {}
    drop_jobs(context.job_queue, gid)
    save_state()
    await refresh_messages(context, gid, force=True)
    await msg.reply_text(
        f"✅ Giveaway <code>{html.escape(gid)}</code> cancelled.", parse_mode=ParseMode.HTML
    )


# ==================== JOINING ====================
async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = query.data.split("_", 1)[1]
    gw = giveaways.get(gid)

    # NOTE: a callback query can only be answered once, so every path below answers exactly once.
    if not gw or gw["status"] != "active" or now_utc() >= parse_iso(gw["end_time"]):
        await query.answer("This giveaway has ended.", show_alert=True)
        return

    user = query.from_user
    uid = str(user.id)

    if uid in gw["participants"]:
        await query.answer("You're already in! 🍀", show_alert=True)
        return

    if gw["channels"]:
        ok, missing = await check_user_in_channels(context.bot, user.id, gw["channels"])
        if not ok:
            await query.answer(
                ("To enter, first join:\n" + "\n".join(missing))[:200], show_alert=True
            )
            return

    gw["participants"][uid] = {"name": user.full_name, "username": user.username}
    save_state()
    await query.answer("✅ You're in! Good luck 🍀", show_alert=True)

    # Update the counter right away (throttled so busy giveaways don't hit rate limits)
    await refresh_messages(context, gid, min_interval=5.0)


# ==================== STARTUP ====================
async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("giveaway", "Host a new giveaway"),
            BotCommand("giveaways", "List active giveaways"),
            BotCommand("endgiveaway", "Draw the winner now"),
            BotCommand("cancelgiveaway", "Cancel a giveaway"),
        ])
    except Exception as e:
        logger.warning("Could not set command menu: %s", e)

    # Resume giveaways that were running when the bot stopped
    for gid, gw in giveaways.items():
        if gw["status"] == "active":
            schedule_giveaway(application.job_queue, gid, parse_iso(gw["end_time"]))
            logger.info("Resumed giveaway %s", gid)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


def main():
    #if not BOT_TOKEN:
       # raise SystemExit("BOT_TOKEN environment variable is not set.")

    load_state()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    if application.job_queue is None:
        raise SystemExit('Missing job queue. Run: pip install "python-telegram-bot[job-queue]"')

    new_messages = filters.UpdateType.MESSAGE  # ignore edited messages
    application.add_handler(CommandHandler("start", start, filters=new_messages))
    application.add_handler(CommandHandler("giveaway", giveaway_command, filters=new_messages))
    # Same command, sent as the caption of a photo
    application.add_handler(MessageHandler(
        new_messages & filters.PHOTO & filters.CaptionRegex(r"^/giveaway(?:@\w+)?(?:\s|$)"),
        giveaway_command,
    ))
    application.add_handler(CommandHandler("giveaways", list_giveaways, filters=new_messages))
    application.add_handler(CommandHandler("endgiveaway", end_giveaway, filters=new_messages))
    application.add_handler(CommandHandler("cancelgiveaway", cancel_giveaway, filters=new_messages))
    application.add_handler(CallbackQueryHandler(join_callback, pattern="^join_"))
    application.add_error_handler(error_handler)

    application.run_polling()


if __name__ == "__main__":
    main()
