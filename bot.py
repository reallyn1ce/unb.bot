import os
import json
import asyncio
import logging
import urllib.parse
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BOT_VERSION = "5.0.0"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "economy_bot")
PORT = int(os.getenv("PORT", "10000"))

# --- Экономика ---------------------------------------------------------------
WORK_MIN, WORK_MAX = 200, 700
WORK_COOLDOWN = 4 * 3600  # секунды; хранится в БД (users.last_work)
WORK_TAX_RATE = 0.13
LOAN_INTEREST = 0.20
LOAN_MIN, LOAN_MAX = 100, 1_000_000
REPORT_HOUR_UTC = 18

# --- Бизнес ------------------------------------------------------------------
BUSINESS_COST = 50_000
BUSINESS_HOURLY_INCOME = 1_000          # доход 1-го уровня
BUSINESS_INCOME_PER_LEVEL = 500         # +500/час за каждый следующий уровень
BUSINESS_PROPERTY_TAX = 0.05            # 5% от дохода -> налог растёт вместе с доходом
BUSINESS_UPGRADE_BASE_COST = 25_000     # цена апгрейда = база * текущий уровень
BUSINESS_MAX_LEVEL = 10

# --- Казино ------------------------------------------------------------------
CASINO_MIN_BET = 10
CASINO_MAX_BET = 100_000
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "💎", "7️⃣"]
BJ_TIMEOUT = 90  # секунд на ход; по таймауту игрок автоматически говорит «Хватит»

# --- ИИ (Groq) ---------------------------------------------------------------
# llama3-70b-8192 отключена Groq 30.08.2025, поэтому по умолчанию используется
# её официальный преемник; старые идентификаторы автоматически подменяются.
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
LEGACY_MODEL_ALIASES = {
    "llama3-70b-8192": "llama-3.3-70b-versatile",
    "llama3-8b-8192": "llama-3.1-8b-instant",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("central_bank")

rng = random.SystemRandom()


class EconomyError(Exception):
    """Ожидаемая ошибка. Если её выбросить внутри транзакции — транзакция откатится,
    а текст ошибки будет показан игроку."""


# --- Утилиты -----------------------------------------------------------------
def now():
    return datetime.now(timezone.utc)


def fmt(value):
    return f"{int(value):,}".replace(",", " ")


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes or hours:
        parts.append(f"{minutes} мин")
    parts.append(f"{secs} с")
    return " ".join(parts)


def mongo_uri():
    user = urllib.parse.quote_plus(MONGO_USER or "")
    password = urllib.parse.quote_plus(MONGO_PASSWORD or "")
    return f"mongodb+srv://{user}:{password}@eco.lphalvu.mongodb.net/?retryWrites=true&w=majority&appName=CentralBank"


def check_env():
    missing = [k for k, v in {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "MONGO_USER": MONGO_USER,
        "MONGO_PASSWORD": MONGO_PASSWORD,
    }.items() if not v]
    if missing:
        raise RuntimeError("Не заданы: " + ", ".join(missing))


def as_utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def business_income(level):
    return BUSINESS_HOURLY_INCOME + BUSINESS_INCOME_PER_LEVEL * (max(1, int(level)) - 1)


def business_tax(level):
    return int(business_income(level) * BUSINESS_PROPERTY_TAX)


def upgrade_cost(level):
    return BUSINESS_UPGRADE_BASE_COST * max(1, int(level))


async def safe_send(target, *args, **kwargs):
    """Отправка сообщения/файлов без падения при нехватке прав или удалённом канале.
    Возвращает отправленное сообщение или None."""
    try:
        return await target.send(*args, **kwargs)
    except discord.Forbidden:
        log.warning("Нет прав на отправку в %r", target)
    except discord.NotFound:
        log.warning("Канал/сообщение не найдены: %r", target)
    except discord.HTTPException:
        log.exception("Discord отклонил сообщение (%r)", target)
    return None


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


# =============================================================================
# ИИ
# =============================================================================
class AIError(Exception):
    pass


class AIHandler:
    """Клиент Groq (OpenAI-совместимый API). Без GROQ_API_KEY ИИ просто отключён."""

    URL = "https://api.groq.com/openai/v1/chat/completions"
    REPORT_PROMPT = (
        "Ты — финансовый аналитик. Проанализируй эти данные экономики и напиши "
        "краткий отчет (3-4 предложения) о состоянии рынка"
    )

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
        self.model = LEGACY_MODEL_ALIASES.get(model, model)
        self._session = None

    @property
    def enabled(self):
        return bool(self.api_key)

    async def _http(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def complete(self, system, user, *, max_tokens=400):
        if not self.enabled:
            raise AIError("GROQ_API_KEY не задан.")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            session = await self._http()
            async with session.post(self.URL, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    log.error("Groq HTTP %s: %s", resp.status, body[:500])
                    raise AIError(f"Groq вернул ошибку HTTP {resp.status}.")
                data = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("Groq недоступен")
            raise AIError("Не удалось связаться с Groq.")
        except json.JSONDecodeError:
            raise AIError("Groq вернул некорректный ответ.")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError):
            raise AIError("Groq вернул пустой ответ.")

    async def market_report(self, stats):
        data = json.dumps(stats, ensure_ascii=False)
        return await self.complete(self.REPORT_PROMPT, f"Данные экономики (JSON):\n{data}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# Бот
# =============================================================================
class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
            activity=discord.Game(name="!balance | !cb"),
        )
        self.mongo = None
        self.db = None
        self.db_users = None
        self.businesses = None
        self.economy = None
        self.tx = None
        self.funds = None
        self.history = None
        self.settings = None
        self.ai = AIHandler()
        self.chart_lock = asyncio.Lock()
        self._web_runner = None
        self.chart_files = {
            "all": "/tmp/cb_economy_all.png",
            "24h": "/tmp/cb_economy_24h.png",
        }

    async def close(self):
        try:
            await self.ai.close()
        except Exception:
            log.exception("Ошибка закрытия AI-сессии")
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        if self.mongo is not None:
            self.mongo.close()
            self.mongo = None
        await super().close()

    async def setup_hook(self):
        # Render Web Service requires an HTTP listener on the assigned PORT.
        app = web.Application()
        async def health(request):
            return web.json_response({"status": "ok", "service": "discord-economy-bot"})
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, "0.0.0.0", PORT)
        await site.start()
        log.info("HTTP health server listening on port %s", PORT)

        self.mongo = AsyncIOMotorClient(mongo_uri(), serverSelectionTimeoutMS=10000)
        await self.mongo.admin.command("ping")
        self.db = self.mongo[MONGO_DB_NAME]
        self.db_users = self.db.users
        self.businesses = self.db.businesses
        self.economy = self.db.economy
        self.tx = self.db.transactions
        self.funds = self.db.funds
        self.history = self.db.economy_history
        self.settings = self.db.guild_settings

        await self.history.create_index("created_at")
        await self.tx.create_index("created_at")
        await self.db_users.create_index("cash")
        await self.db_users.create_index("bank")
        await self.economy.update_one(
            {"_id": "central_bank"},
            {"$setOnInsert": {"reserve": 0, "printed": 0, "created_at": now()},
             "$set": {"updated_at": now()}},
            upsert=True,
        )
        await self.funds.update_one(
            {"_id": "state_budget"},
            {"$setOnInsert": {"name": "Государственный бюджет", "balance": 0, "created_at": now()},
             "$set": {"updated_at": now()}},
            upsert=True,
        )
        await self.add_cog(EconomyCog(self))
        await self.add_cog(CasinoCog(self))
        await self.add_cog(AICog(self))
        if not self.ai.enabled:
            log.info("GROQ_API_KEY не задан — ИИ-функции отключены.")
        try:
            synced = await self.tree.sync()
            log.info("Синхронизировано slash-команд: %s", len(synced))
        except Exception:
            log.exception("Slash sync failed")
        self.business_loop.start()
        self.snapshot_loop.start()
        self.daily_report_loop.start()

    async def close(self):
        for loop in (self.business_loop, self.snapshot_loop, self.daily_report_loop):
            if loop.is_running():
                loop.cancel()
        await self.ai.close()
        if self.mongo:
            self.mongo.close()
        await super().close()

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            text = "❌ Эта команда доступна только администраторам."
        elif isinstance(error, commands.NoPrivateMessage):
            text = "❌ Эта команда работает только на сервере."
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                                commands.BadUnionArgument)):
            text = "❌ Неверные аргументы. Список команд: `!help`."
        elif isinstance(error, commands.CheckFailure):
            text = "❌ Команда недоступна."
        else:
            log.error("Ошибка команды %s", ctx.command, exc_info=error)
            text = "⚠️ Внутренняя ошибка. Попробуй ещё раз чуть позже."
        await safe_send(ctx, text)

    # --- Работа с БД ---------------------------------------------------------
    async def ensure_user(self, user_id: int, *, session=None):
        await self.db_users.update_one(
            {"_id": int(user_id)},
            {"$setOnInsert": {"cash": 0, "bank": 0, "debt": 0, "last_work": None, "created_at": now()}},
            upsert=True, session=session,
        )
        return await self.db_users.find_one({"_id": int(user_id)}, session=session)

    async def get_balance(self, user_id: int):
        return await self.ensure_user(user_id)

    async def change_cash(self, user_id: int, delta: int, *, session=None):
        """Атомарное изменение наличных. Отрицательное изменение не уводит cash в минус."""
        delta = int(delta)
        await self.ensure_user(user_id, session=session)
        if delta < 0:
            result = await self.db_users.update_one(
                {"_id": int(user_id), "cash": {"$gte": -delta}},
                {"$inc": {"cash": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        else:
            result = await self.db_users.update_one(
                {"_id": int(user_id)},
                {"$inc": {"cash": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        return result.modified_count == 1

    async def change_bank(self, user_id: int, delta: int, *, session=None):
        delta = int(delta)
        await self.ensure_user(user_id, session=session)
        if delta < 0:
            result = await self.db_users.update_one(
                {"_id": int(user_id), "bank": {"$gte": -delta}},
                {"$inc": {"bank": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        else:
            result = await self.db_users.update_one(
                {"_id": int(user_id)},
                {"$inc": {"bank": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        return result.modified_count == 1

    async def journal(self, source, destination, amount, reason, meta=None, *, session=None):
        await self.tx.insert_one({
            "source": str(source), "destination": str(destination),
            "amount": int(amount), "reason": reason,
            "meta": meta or {}, "created_at": now(),
        }, session=session)

    async def transfer_players(self, sender_id: int, recipient_id: int, amount: int):
        amount = int(amount)
        if amount <= 0 or sender_id == recipient_id:
            return False, "Сумма должна быть положительной, а получатель — другим игроком."
        await self.ensure_user(sender_id)
        await self.ensure_user(recipient_id)
        try:
            # MongoDB transactions require a replica set/sharded cluster (Atlas supports this).
            async with await self.mongo.start_session() as session:
                async with session.start_transaction():
                    sender = await self.db_users.find_one({"_id": sender_id}, session=session)
                    if int(sender.get("debt", 0)) > 0:
                        raise EconomyError("У тебя есть долг. Погаси его перед переводами.")
                    if int(sender.get("cash", 0)) < amount:
                        raise EconomyError("Недостаточно наличных.")
                    debited = await self.db_users.update_one(
                        {"_id": sender_id, "cash": {"$gte": amount}, "debt": 0},
                        {"$inc": {"cash": -amount}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if debited.modified_count != 1:
                        raise EconomyError("Перевод отклонён: проверь баланс и долг.")
                    await self.db_users.update_one(
                        {"_id": recipient_id},
                        {"$inc": {"cash": amount}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    await self.journal(f"user_{sender_id}", f"user_{recipient_id}", amount,
                                       "Перевод между игроками",
                                       {"type": "player_transfer"}, session=session)
        except EconomyError as e:
            return False, str(e)
        return True, "Перевод выполнен."

    async def fund_balance(self, name="state_budget"):
        doc = await self.funds.find_one({"_id": name})
        return int((doc or {}).get("balance", 0))

    async def add_budget(self, amount: int, reason: str, *, session=None):
        amount = int(amount)
        if amount <= 0:
            return
        await self.funds.update_one(
            {"_id": "state_budget"},
            {"$inc": {"balance": amount}, "$set": {"updated_at": now()},
             "$setOnInsert": {"name": "Государственный бюджет", "created_at": now()}},
            upsert=True, session=session,
        )
        await self.journal("taxpayer", "fund_state_budget", amount, reason, session=session)

    async def _pay_earning_tx(self, session, user_id, gross, tax, net, reason, account):
        await self.db_users.update_one(
            {"_id": user_id},
            {"$inc": {account: net}, "$set": {"updated_at": now()}},
            session=session,
        )
        await self.add_budget(tax, f"НДФЛ 13%: {reason}", session=session)
        await self.journal("economy", f"user_{user_id}", net, reason,
                           {"gross": gross, "tax": tax, "tax_rate": WORK_TAX_RATE},
                           session=session)

    async def pay_earning(self, user_id: int, gross: int, reason: str, *, account="cash", session=None):
        """Начисляет доход за вычетом НДФЛ. Если передана session — работает внутри
        уже открытой транзакции вызывающего (вложенные транзакции в MongoDB запрещены)."""
        gross = max(0, int(gross))
        tax = int(gross * WORK_TAX_RATE)
        net = gross - tax
        await self.ensure_user(user_id, session=session)
        if session is not None:
            await self._pay_earning_tx(session, user_id, gross, tax, net, reason, account)
        else:
            async with await self.mongo.start_session() as own_session:
                async with own_session.start_transaction():
                    await self._pay_earning_tx(own_session, user_id, gross, tax, net, reason, account)
        return gross, tax, net

    async def reserve(self, *, session=None):
        doc = await self.economy.find_one({"_id": "central_bank"}, session=session)
        return int((doc or {}).get("reserve", 0))

    async def change_reserve(self, delta: int, *, session=None, allow_negative=False):
        """Изменение резерва ЦБ. Списание блокируется, если не хватает резерва,
        кроме случая allow_negative=True (выплата уже принятых ставок)."""
        delta = int(delta)
        if delta < 0 and not allow_negative:
            result = await self.economy.update_one(
                {"_id": "central_bank", "reserve": {"$gte": -delta}},
                {"$inc": {"reserve": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        else:
            result = await self.economy.update_one(
                {"_id": "central_bank"},
                {"$inc": {"reserve": delta}, "$set": {"updated_at": now()}},
                session=session,
            )
        return result.modified_count == 1

    async def stats(self):
        reserve = await self.reserve()
        budget = await self.fund_balance("state_budget")
        fund_rows = await self.funds.aggregate([{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]).to_list(1)
        funds_total = int(fund_rows[0]["total"]) if fund_rows else 0
        players_rows = await self.db_users.aggregate([{"$group": {"_id": None, "cash": {"$sum": "$cash"}, "bank": {"$sum": "$bank"}, "debt": {"$sum": "$debt"}}}]).to_list(1)
        p = players_rows[0] if players_rows else {}
        players = int(p.get("cash", 0)) + int(p.get("bank", 0))
        player_debt = int(p.get("debt", 0))
        central = await self.economy.find_one({"_id": "central_bank"}) or {}
        since = now() - timedelta(hours=24)
        incoming = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "destination": {"$regex": "user_"}}},
            {"$group": {"_id": None, "n": {"$sum": "$amount"}}},
        ]).to_list(1)
        outgoing = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "source": {"$regex": "user_"}}},
            {"$group": {"_id": None, "n": {"$sum": "$amount"}}},
        ]).to_list(1)
        return {
            "reserve": reserve, "funds": funds_total, "budget": budget,
            "players": players, "player_debt": player_debt,
            "circulation": players + funds_total,
            "debt": max(0, -reserve), "supply": reserve + players + funds_total,
            "printed": int(central.get("printed", 0)),
            "incoming": int(incoming[0]["n"]) if incoming else 0,
            "outgoing": int(outgoing[0]["n"]) if outgoing else 0,
        }

    async def snapshot(self):
        stats = await self.stats()
        await self.history.insert_one({
            "created_at": now(), **stats,
            "state_budget": stats["budget"],
        })

    @staticmethod
    def _render_chart(rows, title, path):
        dates = [as_utc(r["created_at"]) for r in rows]
        fig, ax = plt.subplots(figsize=(13, 6.5))
        ax.plot(dates, [int(r.get("reserve", 0)) for r in rows], label="Резерв ЦБ", linewidth=2)
        ax.plot(dates, [int(r.get("circulation", 0)) for r in rows], label="В обращении", linewidth=2)
        ax.plot(dates, [int(r.get("debt", 0)) for r in rows], label="Дефицит ЦБ", linewidth=2)
        ax.plot(dates, [int(r.get("state_budget", r.get("budget", 0))) for r in rows],
                label="Госбюджет", linewidth=2)
        ax.axhline(0, linewidth=1)
        ax.set_title(title)
        ax.set_xlabel("Время")
        ax.set_ylabel("Монеты")
        ax.grid(True, alpha=0.25)
        ax.legend()
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout()
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)

    async def publish_charts(self, channel=None) -> bool:
        """Строит графики и отправляет их в канал. True — если сообщение доставлено."""
        async with self.chart_lock:
            rows = await self.history.find({}).sort("created_at", 1).to_list(100000)
            if not rows:
                return False
            clean = [r for r in rows if as_utc(r.get("created_at"))]
            day = [r for r in clean if as_utc(r["created_at"]) >= now() - timedelta(hours=24)] or clean[-1:]
            try:
                await asyncio.to_thread(self._render_chart, clean[-2500:], "Экономика — вся история", self.chart_files["all"])
                await asyncio.to_thread(self._render_chart, day[-1500:], "Экономика — последние 24 часа", self.chart_files["24h"])
            except Exception:
                log.exception("Не удалось построить графики")
                return False
            if channel is None:
                log.warning("Графики созданы, но канал для отправки не указан.")
                return False
            try:
                files = [discord.File(self.chart_files["all"], filename="economy_all.png"),
                         discord.File(self.chart_files["24h"], filename="economy_24h.png")]
            except OSError:
                log.exception("Файлы графиков недоступны")
                return False
            message = await safe_send(
                channel,
                f"📊 Графики экономики обновлены: <t:{int(now().timestamp())}:R>",
                files=files,
            )
            return message is not None

    @tasks.loop(hours=1)
    async def business_loop(self):
        current = now()
        async for business in self.businesses.find({}):
            last = as_utc(business.get("last_payout"))
            if last and (current - last).total_seconds() < 3600:
                continue
            uid = int(business["_id"])
            level = max(1, int(business.get("level", 1)))
            income = business_income(level)
            tax = business_tax(level)
            try:
                async with await self.mongo.start_session() as session:
                    async with session.start_transaction():
                        await self.ensure_user(uid, session=session)
                        # Налог на имущество списывается с наличных. Нет наличных — бизнес замораживается.
                        debit = await self.db_users.update_one(
                            {"_id": uid, "cash": {"$gte": tax}},
                            {"$inc": {"cash": -tax, "bank": income}, "$set": {"updated_at": current}},
                            session=session,
                        )
                        if debit.modified_count != 1:
                            await self.businesses.update_one(
                                {"_id": uid},
                                {"$set": {"frozen": True, "last_checked": current}},
                                session=session,
                            )
                            continue
                        await self.add_budget(tax, "Налог на имущество бизнеса 5%", session=session)
                        await self.businesses.update_one(
                            {"_id": uid},
                            {"$set": {"last_payout": current, "last_checked": current, "frozen": False}},
                            session=session,
                        )
                        await self.journal(f"business_{uid}", f"user_{uid}", income,
                                           "Доход бизнеса", {"property_tax": tax, "level": level},
                                           session=session)
                log.info("Business payout uid=%s level=%s income=%s tax=%s", uid, level, income, tax)
            except Exception:
                log.exception("Business payout failed for %s", uid)

    @business_loop.before_loop
    async def before_business_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def snapshot_loop(self):
        try:
            await self.snapshot()
        except Exception:
            log.exception("Economy snapshot failed")

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.wait_until_ready()

    async def _resolve_channel(self, guild, channel_id):
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Channel %s not found/accessible", channel_id)
                channel = None
        return channel

    @tasks.loop(minutes=5)
    async def daily_report_loop(self):
        """Отправляет ежедневную сводку и/или графики в настроенные каналы."""
        current = now()
        if current.hour != REPORT_HOUR_UTC:
            return
        date_key = current.strftime("%Y-%m-%d")
        query = {"$or": [
            {"report_channel_id": {"$exists": True}},
            {"chart_channel_id": {"$exists": True}},
        ]}
        async for config in self.settings.find(query):
            if config.get("last_automation_date") == date_key:
                continue
            guild = self.get_guild(int(config["_id"]))
            if guild is None:
                continue
            try:
                stats = await self.stats()
                report_channel_id = config.get("report_channel_id")
                if report_channel_id:
                    channel = await self._resolve_channel(guild, report_channel_id)
                    if channel is not None:
                        await safe_send(
                            channel,
                            "📊 **Ежедневный экономический отчёт**\n"
                            f"🏦 Резерв ЦБ: `{fmt(stats['reserve'])}`\n"
                            f"💰 Госбюджет: `{fmt(stats['budget'])}`\n"
                            f"👥 Средства игроков: `{fmt(stats['players'])}`\n"
                            f"🪙 В обращении: `{fmt(stats['circulation'])}`\n"
                            f"📉 Долги игроков: `{fmt(stats['player_debt'])}`\n"
                            f"↗️ Приход за 24 ч: `{fmt(stats['incoming'])}`\n"
                            f"↘️ Расход за 24 ч: `{fmt(stats['outgoing'])}`\n"
                            f"🕒 Дата отчёта: `{date_key} UTC`"
                        )
                chart_channel_id = config.get("chart_channel_id")
                if chart_channel_id:
                    chart_channel = await self._resolve_channel(guild, chart_channel_id)
                    if chart_channel is not None:
                        await self.snapshot()
                        await self.publish_charts(channel=chart_channel)
                await self.settings.update_one(
                    {"_id": int(config["_id"])},
                    {"$set": {"last_automation_date": date_key}},
                )
            except Exception:
                log.exception("Daily report/graphs failed for guild %s", config.get("_id"))

    @daily_report_loop.before_loop
    async def before_daily_report_loop(self):
        await self.wait_until_ready()


# =============================================================================
# Экономика
# =============================================================================
class EconomyCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Команды экономики")
    async def help_command(self, ctx):
        await safe_send(
            ctx,
            "**Экономика / Центральный банк**\n"
            "`!balance` — баланс; `!pay @user сумма` — перевод\n"
            "`!work` — работа раз в 4 часа; `!top` — топ-10\n"
            "`!business buy` — купить бизнес; `!business status` — статус\n"
            "`!business upgrade` — улучшить бизнес (оплата с банковского счёта)\n"
            "`!loan take сумма`; `!loan pay сумма` — кредит и погашение\n"
            "`!cb`, `!economy` — состояние системы\n"
            "**Казино** (ставки из наличных):\n"
            f"`!slot ставка`, `!bj ставка`, `!rr ставка` (от {fmt(CASINO_MIN_BET)} до {fmt(CASINO_MAX_BET)})\n"
            "**Админ:**\n"
            "`/graphs` или `!graphs` — графики\n"
            "`/setreportchannel`, `/setchartchannel` — каналы отчётов и графиков\n"
            "`!ai_report` — ИИ-отчёт по экономике; `!ai_toggle` — вкл/выкл ИИ на сервере"
        )

    @commands.hybrid_command(name="balance", description="Показать баланс")
    async def balance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        data = await self.bot.get_balance(member.id)
        embed = discord.Embed(title=f"💰 Баланс — {member.display_name}", color=discord.Color.gold())
        embed.add_field(name="Наличные", value=f"`{fmt(data['cash'])}`", inline=True)
        embed.add_field(name="Банк", value=f"`{fmt(data['bank'])}`", inline=True)
        embed.add_field(name="Долг", value=f"`{fmt(data['debt'])}`", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await safe_send(ctx, embed=embed)

    @commands.hybrid_command(name="pay", description="Перевести наличные игроку")
    @app_commands.describe(member="Кому перевести", amount="Сумма перевода")
    async def pay(self, ctx, member: discord.Member, amount: int):
        if member.bot:
            return await safe_send(ctx, "❌ Нельзя переводить деньги ботам.")
        ok, message = await self.bot.transfer_players(ctx.author.id, member.id, amount)
        await safe_send(ctx, ("✅ " if ok else "❌ ") + message)

    @commands.hybrid_command(name="work", description="Поработать и получить зарплату")
    async def work(self, ctx):
        uid = ctx.author.id
        await self.bot.ensure_user(uid)
        current = now()
        threshold = current - timedelta(seconds=WORK_COOLDOWN)
        gross = random.randint(WORK_MIN, WORK_MAX)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    # Атомарная проверка кулдауна: фильтр пропускает только тех, у кого
                    # last_work пуст/отсутствует или старше 4 часов. Гонка двух команд невозможна.
                    claimed = await self.bot.db_users.update_one(
                        {"_id": uid, "$or": [{"last_work": None}, {"last_work": {"$lte": threshold}}]},
                        {"$set": {"last_work": current, "updated_at": current}},
                        session=session,
                    )
                    if claimed.modified_count != 1:
                        raise EconomyError("cooldown")
                    _, tax, net = await self.bot.pay_earning(
                        uid, gross, "Зарплата за работу", session=session)
        except EconomyError:
            doc = await self.bot.db_users.find_one({"_id": uid}) or {}
            last = as_utc(doc.get("last_work")) or current
            remaining = WORK_COOLDOWN - (now() - last).total_seconds()
            return await safe_send(
                ctx, f"⏳ Ты устал. Следующая работа через **{fmt_duration(remaining)}**.")
        await safe_send(
            ctx,
            f"🛠️ Ты заработал **{fmt(gross)}** монет.\n"
            f"НДФЛ (13%): **{fmt(tax)}** в госбюджет.\n"
            f"На руки: **{fmt(net)}** монет."
        )

    @commands.hybrid_command(name="top", description="Топ-10 богатейших игроков")
    async def top(self, ctx):
        rows = await self.bot.db_users.aggregate([
            {"$addFields": {"net": {"$subtract": [
                {"$add": [{"$ifNull": ["$cash", 0]}, {"$ifNull": ["$bank", 0]}]},
                {"$ifNull": ["$debt", 0]}]}}},
            {"$sort": {"net": -1}},
            {"$limit": 10},
        ]).to_list(10)
        lines = []
        for i, row in enumerate(rows, 1):
            uid = int(row["_id"])
            member = ctx.guild.get_member(uid) if ctx.guild else None
            name = member.display_name if member else f"ID {uid}"
            lines.append(f"**{i}.** {name} — `{fmt(row['net'])}`")
        embed = discord.Embed(
            title="🏆 Топ-10 экономики",
            description="\n".join(lines) if lines else "Пока нет игроков.",
            color=discord.Color.gold(),
        )
        await safe_send(ctx, embed=embed)

    # --- Бизнес --------------------------------------------------------------
    @staticmethod
    def _business_text(doc):
        level = int(doc.get("level", 1))
        state = "заморожен" if doc.get("frozen") else "работает"
        text = (
            f"🏢 Бизнес: уровень `{level}`/{BUSINESS_MAX_LEVEL}, статус: **{state}**.\n"
            f"Доход: `{fmt(business_income(level))}`/час; "
            f"налог: `{fmt(business_tax(level))}`/час из наличных (5% дохода)."
        )
        if level < BUSINESS_MAX_LEVEL:
            text += (f"\nУлучшение до уровня {level + 1}: `{fmt(upgrade_cost(level))}` "
                     f"с банковского счёта (`!business upgrade`).")
        else:
            text += "\nДостигнут максимальный уровень."
        return text

    @commands.hybrid_group(name="business", description="Управление бизнесом", invoke_without_command=True)
    async def business(self, ctx):
        doc = await self.bot.businesses.find_one({"_id": ctx.author.id})
        if not doc:
            return await safe_send(ctx, "У тебя пока нет бизнеса. Используй `!business buy`.")
        await safe_send(ctx, self._business_text(doc))

    @business.command(name="buy", description="Купить бизнес")
    async def business_buy(self, ctx):
        uid = ctx.author.id
        user = await self.bot.ensure_user(uid)
        if int(user.get("debt", 0)) > 0:
            return await safe_send(ctx, "❌ Сначала полностью погаси долг, затем покупай бизнес.")
        if await self.bot.businesses.find_one({"_id": uid}):
            return await safe_send(ctx, "❌ У тебя уже есть бизнес.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    result = await self.bot.db_users.update_one(
                        {"_id": uid, "cash": {"$gte": BUSINESS_COST}, "debt": 0},
                        {"$inc": {"cash": -BUSINESS_COST}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if result.modified_count != 1:
                        raise EconomyError(f"Нужно {fmt(BUSINESS_COST)} наличными и отсутствие долга.")
                    await self.bot.businesses.insert_one({
                        "_id": uid, "level": 1, "last_payout": now(),
                        "frozen": False, "created_at": now(),
                    }, session=session)
                    await self.bot.journal(f"user_{uid}", "business_market", BUSINESS_COST,
                                           "Покупка бизнеса", session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"✅ Бизнес куплен за **{fmt(BUSINESS_COST)}**. Первый доход начислится примерно через час.")

    @business.command(name="status", description="Статус бизнеса")
    async def business_status(self, ctx):
        doc = await self.bot.businesses.find_one({"_id": ctx.author.id})
        if not doc:
            return await safe_send(ctx, "У тебя пока нет бизнеса. Используй `!business buy`.")
        await safe_send(ctx, self._business_text(doc))

    @business.command(name="upgrade", description="Улучшить бизнес (+500 монет/час за уровень)")
    async def business_upgrade(self, ctx):
        uid = ctx.author.id
        await self.bot.ensure_user(uid)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    doc = await self.bot.businesses.find_one({"_id": uid}, session=session)
                    if not doc:
                        raise EconomyError("У тебя пока нет бизнеса. Используй `!business buy`.")
                    level = int(doc.get("level", 1))
                    if level >= BUSINESS_MAX_LEVEL:
                        raise EconomyError("Бизнес уже достиг максимального уровня.")
                    cost = upgrade_cost(level)
                    if not await self.bot.change_bank(uid, -cost, session=session):
                        raise EconomyError(f"Для улучшения нужно `{fmt(cost)}` на банковском счёте.")
                    upgraded = await self.bot.businesses.update_one(
                        {"_id": uid, "level": level},
                        {"$inc": {"level": 1}, "$set": {"upgraded_at": now()}},
                        session=session,
                    )
                    if upgraded.modified_count != 1:
                        raise EconomyError("Бизнес изменился во время операции, попробуй ещё раз.")
                    # Плата за улучшение возвращается в резерв ЦБ.
                    await self.bot.change_reserve(cost, session=session)
                    await self.bot.journal(f"user_{uid}", "central_bank", cost,
                                           "Улучшение бизнеса",
                                           {"from_level": level, "to_level": level + 1},
                                           session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        new_level = level + 1
        await safe_send(
            ctx,
            f"⬆️ Бизнес улучшен до уровня **{new_level}** за **{fmt(cost)}**.\n"
            f"Доход: `{fmt(business_income(new_level))}`/час, "
            f"налог: `{fmt(business_tax(new_level))}`/час."
        )

    # --- Кредиты -------------------------------------------------------------
    @commands.hybrid_group(name="loan", description="Кредиты", invoke_without_command=True)
    async def loan(self, ctx):
        data = await self.bot.get_balance(ctx.author.id)
        await safe_send(ctx, f"💳 Твой долг: `{fmt(data['debt'])}`. Используй `!loan take сумма` или `!loan pay сумма`.")

    @loan.command(name="take", description="Взять кредит из резерва ЦБ")
    @app_commands.describe(amount="Сумма кредита до начисления процентов")
    async def loan_take(self, ctx, amount: int):
        if amount < LOAN_MIN or amount > LOAN_MAX:
            return await safe_send(ctx, f"❌ Сумма должна быть от {fmt(LOAN_MIN)} до {fmt(LOAN_MAX)}.")
        uid = ctx.author.id
        user = await self.bot.ensure_user(uid)
        if int(user.get("debt", 0)) > 0:
            return await safe_send(ctx, "❌ Новый кредит недоступен, пока не погашен текущий долг.")
        owed = int(amount * (1 + LOAN_INTEREST))
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.change_reserve(-amount, session=session):
                        raise EconomyError("В резерве ЦБ недостаточно средств.")
                    taken = await self.bot.db_users.update_one(
                        {"_id": uid, "debt": 0},
                        {"$inc": {"cash": amount, "debt": owed}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if taken.modified_count != 1:
                        raise EconomyError("Новый кредит недоступен, пока не погашен текущий долг.")
                    await self.bot.journal("central_bank", f"user_{uid}", amount,
                                           "Выдача кредита", {"repayment_due": owed}, session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"✅ Получено **{fmt(amount)}**. К погашению: **{fmt(owed)}** (процент 20%).")

    @loan.command(name="pay", description="Погасить кредит наличными")
    @app_commands.describe(amount="Сумма погашения")
    async def loan_pay(self, ctx, amount: int):
        if amount <= 0:
            return await safe_send(ctx, "❌ Сумма должна быть положительной.")
        uid = ctx.author.id
        user = await self.bot.ensure_user(uid)
        debt = int(user.get("debt", 0))
        if debt <= 0:
            return await safe_send(ctx, "У тебя нет долга.")
        payment = min(amount, debt)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    # Условный апдейт: ни cash, ни debt не могут уйти в минус.
                    result = await self.bot.db_users.update_one(
                        {"_id": uid, "cash": {"$gte": payment}, "debt": {"$gte": payment}},
                        {"$inc": {"cash": -payment, "debt": -payment}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if result.modified_count != 1:
                        raise EconomyError("Недостаточно наличных для такого платежа.")
                    await self.bot.change_reserve(payment, session=session)
                    await self.bot.journal(f"user_{uid}", "central_bank", payment,
                                           "Погашение кредита", session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"✅ Погашено **{fmt(payment)}**. Остаток долга: **{fmt(debt - payment)}**.")

    # --- ЦБ ------------------------------------------------------------------
    @commands.hybrid_command(name="cb", description="Состояние Центрального банка")
    async def cb(self, ctx):
        s = await self.bot.stats()
        await safe_send(
            ctx,
            f"🏦 **Центральный банк**\nРезерв: `{fmt(s['reserve'])}`\n"
            f"Фонды: `{fmt(s['funds'])}`\nГосбюджет: `{fmt(s['budget'])}`\n"
            f"Игроки: `{fmt(s['players'])}`\nВ обращении: `{fmt(s['circulation'])}`\n"
            f"Дефицит ЦБ: `{fmt(s['debt'])}`\nНапечатано: `{fmt(s['printed'])}`"
        )

    @commands.hybrid_command(name="economy", description="Статистика экономики")
    async def economy(self, ctx):
        s = await self.bot.stats()
        await safe_send(
            ctx,
            f"📈 **Экономика**\nРезерв `{fmt(s['reserve'])}` | Фонды `{fmt(s['funds'])}`\n"
            f"Госбюджет `{fmt(s['budget'])}`\nИгроки `{fmt(s['players'])}` | "
            f"В обращении `{fmt(s['circulation'])}`\nДолги игроков `{fmt(s['player_debt'])}`\n"
            f"За 24ч: приход `{fmt(s['incoming'])}` / расход `{fmt(s['outgoing'])}`"
        )

    @commands.hybrid_command(name="print_money", description="Эмиссия средств в резерв ЦБ")
    @commands.has_permissions(administrator=True)
    async def print_money(self, ctx, amount: int):
        if amount <= 0:
            return await safe_send(ctx, "❌ Сумма должна быть положительной.")
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.bot.change_reserve(amount, session=session)
                await self.bot.economy.update_one(
                    {"_id": "central_bank"}, {"$inc": {"printed": amount}}, session=session
                )
                await self.bot.journal("money_printing", "central_bank", amount, "Эмиссия", session=session)
        await safe_send(ctx, f"✅ Эмиссия: `{fmt(amount)}`. Резерв: `{fmt(await self.bot.reserve())}`")

    @commands.hybrid_command(name="burn_money", description="Сжечь средства из резерва ЦБ")
    @commands.has_permissions(administrator=True)
    async def burn_money(self, ctx, amount: int):
        if amount <= 0:
            return await safe_send(ctx, "❌ Сумма должна быть положительной.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.change_reserve(-amount, session=session):
                        raise EconomyError("В резерве недостаточно средств.")
                    await self.bot.journal("central_bank", "money_burn", amount,
                                           "Сжигание денег", session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"🔥 Сожжено `{fmt(amount)}`.")

    @commands.hybrid_command(name="setreportchannel", description="Настроить канал ежедневных отчётов")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(channel="Канал для ежедневной сводки (по умолчанию текущий)")
    async def setreportchannel(self, ctx, channel: discord.TextChannel = None):
        if ctx.guild is None:
            return await safe_send(ctx, "❌ Эту настройку можно менять только на сервере.")
        channel = channel or ctx.channel
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"report_channel_id": channel.id, "updated_at": now()}},
            upsert=True,
        )
        await safe_send(ctx, f"✅ Ежедневные отчёты будут отправляться в {channel.mention} примерно в {REPORT_HOUR_UTC:02d}:00 UTC.")

    @commands.hybrid_command(name="setchartchannel", description="Настроить канал автоматических графиков")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(channel="Канал для ежедневных графиков (по умолчанию текущий)")
    async def setchartchannel(self, ctx, channel: discord.TextChannel = None):
        if ctx.guild is None:
            return await safe_send(ctx, "❌ Эту настройку можно менять только на сервере.")
        channel = channel or ctx.channel
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"chart_channel_id": channel.id, "updated_at": now()}},
            upsert=True,
        )
        await safe_send(ctx, f"✅ Ежедневные графики будут отправляться в {channel.mention} после отчёта.")

    @commands.hybrid_command(name="graphs", aliases=["chart"], description="Сформировать графики экономики")
    @commands.has_permissions(administrator=True)
    async def graphs(self, ctx):
        await ctx.defer()
        await self.bot.snapshot()
        delivered = await self.bot.publish_charts(channel=ctx.channel)
        if delivered:
            return await safe_send(ctx, "✅ Графики отправлены в этот канал.")
        await safe_send(ctx, "⚠️ Не удалось отправить графики. Проверь у бота права "
                             "«Отправлять сообщения» и «Прикреплять файлы» в этом канале.")


# =============================================================================
# Казино
# =============================================================================
def draw_card():
    return rng.randint(2, 11)


def hand_value(cards):
    """Сумма очков. Карта 11 — «туз»: если перебор, она считается за 1."""
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def fmt_cards(cards, hide_second=False):
    if hide_second:
        return f"`{cards[0]}` `?`"
    return " ".join(f"`{c}`" for c in cards) + f" = **{hand_value(cards)}**"


class BlackjackView(discord.ui.View):
    def __init__(self, cog, user_id: int, bet: int):
        super().__init__(timeout=BJ_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.bet = bet
        self.player = [draw_card(), draw_card()]
        self.dealer = [draw_card(), draw_card()]
        self.lock = asyncio.Lock()
        self.done = False
        self.message = None

    def render(self, result=None, color=None):
        embed = discord.Embed(
            title=f"🃏 Блэкджек — ставка {fmt(self.bet)}",
            color=color or discord.Color.gold(),
        )
        embed.add_field(name="Твои карты", value=fmt_cards(self.player), inline=False)
        embed.add_field(name="Дилер", value=fmt_cards(self.dealer, hide_second=result is None), inline=False)
        if result:
            embed.add_field(name="Итог", value=result, inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message("Это не твоя игра.", ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return False
        return True

    async def _show(self, interaction, embed):
        try:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            elif self.message is not None:
                await self.message.edit(embed=embed, view=self)
        except (discord.Forbidden, discord.NotFound):
            log.warning("Не удалось обновить сообщение блэкджека (нет прав или оно удалено)")
        except discord.HTTPException:
            log.exception("Ошибка обновления сообщения блэкджека")

    async def _finish(self, interaction, outcome):
        """Завершает игру и рассчитывает выплату. Вызывать с захваченным self.lock."""
        self.done = True
        self.stop()
        if outcome == "bust":
            payout, text, color = 0, "💥 Перебор! Ставка проиграна.", discord.Color.red()
        elif outcome == "twentyone":
            payout, text, color = self.bet * 2, "🎉 Ровно 21! Выплата x2.", discord.Color.green()
        else:
            while hand_value(self.dealer) < 17:
                self.dealer.append(draw_card())
            p, d = hand_value(self.player), hand_value(self.dealer)
            if d > 21 or p > d:
                payout, text, color = self.bet * 2, "✅ Ты победил! Выплата x2.", discord.Color.green()
            elif p == d:
                payout, text, color = self.bet, "🤝 Ничья — ставка возвращена.", discord.Color.light_grey()
            else:
                payout, text, color = 0, "❌ Дилер победил. Ставка проиграна.", discord.Color.red()
        if payout > 0:
            try:
                await self.cog.settle_payout(self.user_id, payout, "blackjack")
                text += f"\nВыплата: **{fmt(payout)}**."
            except Exception:
                log.exception("Не удалось выплатить выигрыш blackjack uid=%s payout=%s",
                              self.user_id, payout)
                text += "\n⚠️ Ошибка выплаты — обратись к администратору."
        self.cog.active_bj.discard(self.user_id)
        for child in self.children:
            child.disabled = True
        await self._show(interaction, self.render(text, color))

    @discord.ui.button(label="Взять карту", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            self.player.append(draw_card())
            value = hand_value(self.player)
            if value > 21:
                await self._finish(interaction, "bust")
            elif value == 21:
                await self._finish(interaction, "twentyone")
            else:
                await self._show(interaction, self.render())

    @discord.ui.button(label="Хватит", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            await self._finish(interaction, "stand")

    async def on_timeout(self):
        async with self.lock:
            if not self.done:
                await self._finish(None, "stand")


class CasinoCog(commands.Cog):
    """Все ставки идут в резерв ЦБ, все выигрыши выплачиваются из резерва ЦБ."""

    def __init__(self, bot: EconomyBot):
        self.bot = bot
        self.active_bj = set()  # игроки с незавершённой партией (эфемерное состояние)

    async def precheck(self, uid: int, bet: int):
        if bet < CASINO_MIN_BET or bet > CASINO_MAX_BET:
            raise EconomyError(f"Ставка должна быть от {fmt(CASINO_MIN_BET)} до {fmt(CASINO_MAX_BET)}.")
        user = await self.bot.ensure_user(uid)
        cash = int(user.get("cash", 0))
        if cash < bet:
            raise EconomyError(f"Недостаточно наличных: у тебя `{fmt(cash)}`, ставка `{fmt(bet)}`.")

    async def take_bet(self, uid: int, bet: int, game: str, *, max_payout: int, session):
        """Списывает ставку с cash в резерв ЦБ. Отказывает, если резерв (вместе со ставкой)
        не покроет максимально возможную выплату."""
        debit = await self.bot.db_users.update_one(
            {"_id": uid, "cash": {"$gte": bet}},
            {"$inc": {"cash": -bet}, "$set": {"updated_at": now()}},
            session=session,
        )
        if debit.modified_count != 1:
            raise EconomyError("Недостаточно наличных.")
        await self.bot.change_reserve(bet, session=session)
        if await self.bot.reserve(session=session) < max_payout:
            raise EconomyError("Резерв ЦБ слишком мал, чтобы покрыть возможный выигрыш. "
                               "Уменьши ставку или попроси админа сделать эмиссию.")
        await self.bot.journal(f"user_{uid}", "central_bank", bet, f"Казино: ставка ({game})",
                               {"game": game}, session=session)

    async def pay_out(self, uid: int, payout: int, game: str, *, session, force=False):
        """Выплата из резерва ЦБ. force=True — для уже принятых ставок (резерв может уйти в дефицит)."""
        if payout <= 0:
            return
        if not await self.bot.change_reserve(-payout, session=session, allow_negative=force):
            raise EconomyError("Резерв ЦБ не может покрыть выигрыш.")
        await self.bot.change_cash(uid, payout, session=session)
        await self.bot.journal("central_bank", f"user_{uid}", payout, f"Казино: выплата ({game})",
                               {"game": game}, session=session)

    async def settle_payout(self, uid: int, payout: int, game: str):
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.pay_out(uid, payout, game, session=session, force=True)

    # --- Слоты ---------------------------------------------------------------
    @commands.hybrid_command(name="slot", description="Слоты: 3 одинаковых x5, 2 одинаковых x1.5")
    @app_commands.describe(bet="Ставка наличными")
    async def slot(self, ctx, bet: int):
        uid = ctx.author.id
        try:
            await self.precheck(uid, bet)
            reels = [rng.choice(SLOT_SYMBOLS) for _ in range(3)]
            best = max(Counter(reels).values())
            payout = bet * 5 if best == 3 else (bet * 3) // 2 if best == 2 else 0
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    await self.take_bet(uid, bet, "slot", max_payout=bet * 5, session=session)
                    await self.pay_out(uid, payout, "slot", session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        line = " | ".join(reels)
        if best == 3:
            result = f"🎉 **Джекпот!** Выплата x5: **{fmt(payout)}**."
        elif best == 2:
            result = f"👍 Две одинаковых. Выплата x1.5: **{fmt(payout)}**."
        else:
            result = f"💸 Нет совпадений. Ставка **{fmt(bet)}** сгорела."
        await safe_send(ctx, f"🎰 **[ {line} ]**\n{result}")

    # --- Русская рулетка -----------------------------------------------------
    @commands.hybrid_command(name="rr", description="Русская рулетка: шанс 1 к 6, выигрыш x5")
    @app_commands.describe(bet="Ставка наличными")
    async def rr(self, ctx, bet: int):
        uid = ctx.author.id
        try:
            await self.precheck(uid, bet)
            won = rng.randint(1, 6) == 1
            payout = bet * 5 if won else 0
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    await self.take_bet(uid, bet, "russian_roulette", max_payout=bet * 5, session=session)
                    await self.pay_out(uid, payout, "russian_roulette", session=session)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        if won:
            await safe_send(ctx, f"🔫 *щёлк…* Тебе повезло! Забираешь x5: **{fmt(payout)}**.")
        else:
            await safe_send(ctx, f"🔫 **БАХ!** Ты потерял ставку **{fmt(bet)}**.")

    # --- Блэкджек ------------------------------------------------------------
    @commands.hybrid_command(name="bj", description="Блэкджек: набери 21, дилер берёт до 17")
    @app_commands.describe(bet="Ставка наличными")
    async def bj(self, ctx, bet: int):
        uid = ctx.author.id
        if uid in self.active_bj:
            return await safe_send(ctx, "❌ У тебя уже идёт партия в блэкджек.")
        self.active_bj.add(uid)
        try:
            await self.precheck(uid, bet)
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    await self.take_bet(uid, bet, "blackjack", max_payout=bet * 2, session=session)
        except EconomyError as e:
            self.active_bj.discard(uid)
            return await safe_send(ctx, f"❌ {e}")
        except Exception:
            self.active_bj.discard(uid)
            raise

        view = BlackjackView(self, uid, bet)
        message = await safe_send(ctx, embed=view.render(), view=view)
        if message is None:
            # Не смогли показать игру (нет прав/канал удалён) — возвращаем ставку.
            try:
                await self.settle_payout(uid, bet, "blackjack_refund")
            except Exception:
                log.exception("Не удалось вернуть ставку blackjack uid=%s bet=%s", uid, bet)
            self.active_bj.discard(uid)
            return
        view.message = message
        if hand_value(view.player) == 21:
            async with view.lock:
                await view._finish(None, "twentyone")


# =============================================================================
# ИИ-команды
# =============================================================================
class AICog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot

    async def guild_ai_enabled(self, guild_id: int) -> bool:
        doc = await self.bot.settings.find_one({"_id": int(guild_id)}) or {}
        return bool(doc.get("ai_enabled", True))

    @commands.hybrid_command(name="ai_toggle", description="Включить/выключить ИИ-отчёты на этом сервере")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def ai_toggle(self, ctx):
        new_state = not await self.guild_ai_enabled(ctx.guild.id)
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"ai_enabled": new_state, "updated_at": now()}},
            upsert=True,
        )
        note = ""
        if new_state and not self.bot.ai.enabled:
            note = "\n⚠️ Но на стороне бота не задан `GROQ_API_KEY`, поэтому ИИ фактически недоступен."
        await safe_send(ctx, f"🤖 ИИ-отчёты на этом сервере: **{'включены' if new_state else 'выключены'}**.{note}")

    @commands.hybrid_command(name="ai_report", description="ИИ-отчёт о состоянии экономики")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def ai_report(self, ctx):
        if not self.bot.ai.enabled:
            return await safe_send(ctx, "🤖 ИИ отключён: у бота не задан `GROQ_API_KEY`.")
        if not await self.guild_ai_enabled(ctx.guild.id):
            return await safe_send(ctx, "🤖 ИИ-отчёты выключены на этом сервере. Включить: `!ai_toggle`.")
        await ctx.defer()
        stats = await self.bot.stats()
        try:
            text = await self.bot.ai.market_report(stats)
        except AIError as e:
            return await safe_send(ctx, f"⚠️ Не удалось получить ИИ-отчёт: {e}")
        if len(text) > 1900:
            text = text[:1900].rstrip() + "…"
        await safe_send(ctx, f"🤖 **ИИ-отчёт по экономике**\n{text}")


def main():
    check_env()
    bot = EconomyBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
