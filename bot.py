import os
import json
import asyncio
import logging
import urllib.parse
import random
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError, OperationFailure

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BOT_VERSION = "6.0.1"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "economy_bot")
PORT = int(os.getenv("PORT", "10000"))

# --- Экономика ---------------------------------------------------------------
# Всё, что зависит от сервера (кулдауны, шансы, налоги, зарплата, кредит), хранится
# в guild_settings.eco и описано в PRESETS ниже. Здесь — только то, что не настраивается.
LOAN_MIN, LOAN_MAX = 100, 1_000_000
LOAN_TERM = 3 * 24 * 3600               # срок кредита; после него начисляется штраф за просрочку
REPORT_HOUR_UTC = 18
COOLDOWN_MAX = 7 * 24 * 3600            # верхняя граница кулдауна

# --- Криминал ----------------------------------------------------------------
CRIME_REWARD = (300, 900)               # награда за !crime (из резерва ЦБ, без НДФЛ)
SLUT_REWARD = (200, 700)                # награда за !slut (из резерва ЦБ, без НДФЛ)
CRIME_FINE_RATE = 0.20                  # штраф: 20% от (наличные + банк)...
CRIME_FINE_MAX = 500                    # ...но не больше 500; уходит в госбюджет
ROB_LOOT_RATE = (0.10, 0.30)            # доля наличных жертвы, которую можно украсть
ROB_MAX_LOOT = 5_000
ROB_MIN_TARGET_CASH = 500               # у жертвы должно быть хотя бы столько наличных
ROB_FINE_RATE = 0.25                    # при провале вор платит жертве 25% своих наличных...
ROB_FINE_MAX = 1_000                    # ...но не больше 1000
ROB_VICTIM_PROTECTION = 3600            # после успешного ограбления жертву час нельзя грабить

# --- Бизнес ------------------------------------------------------------------
BUSINESS_COST = 50_000
BUSINESS_HOURLY_INCOME = 1_000          # доход 1-го уровня
BUSINESS_INCOME_PER_LEVEL = 500         # +500/час за каждый следующий уровень
BUSINESS_UPGRADE_BASE_COST = 25_000     # цена апгрейда = база * текущий уровень
BUSINESS_MAX_LEVEL = 10

# --- Казино ------------------------------------------------------------------
CASINO_MIN_BET = 10
CASINO_MAX_BET = 100_000
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "💎", "7️⃣"]
PLAY_AGAIN_TIMEOUT = 120  # сколько кнопка «Играть снова» живёт без нажатий
COIN_EMOJI = "🪙"          # можно заменить на кастомный эмодзи сервера: "<:coin:123456789>"
BJ_TIMEOUT = 90  # секунд на ход; по таймауту игрок автоматически говорит «Хватит»

# --- Настройки сервера: пресеты ----------------------------------------------
# Хранятся в guild_settings.<guild_id>.eco. Для нового сервера действует «Стандартная».
# Значения, которых не было в вашем ТЗ (помечены ↓), одинаковы по смыслу и легко меняются здесь.
_H = 3600
PRESETS = {
    "easy": {
        "label": "🟢 Лёгкая",
        "description": "Без кулдаунов и налогов, высокие шансы",
        "values": {
            "cooldown_work": 0, "cooldown_crime": 0, "cooldown_slut": 0,
            "cooldown_rob": 0, "cooldown_daily": 0,
            "crime_chance": 0.90, "rob_chance": 0.80,
            "tax_rate": 0.0, "work_min": 500, "work_max": 1500,
            "loan_interest": 0.0, "loan_penalty": 0.0,
            "business_tax": 0.0,                      # ↓ налог на имущество бизнеса
            "daily_min": 300, "daily_max": 500,       # ↓ ежедневный бонус
        },
    },
    "standard": {
        "label": "🟡 Стандартная",
        "description": "Сбалансированная экономика (по умолчанию)",
        "values": {
            "cooldown_work": 4 * _H, "cooldown_crime": 2 * _H, "cooldown_slut": 3 * _H,
            "cooldown_rob": 3 * _H, "cooldown_daily": 24 * _H,
            "crime_chance": 0.60, "rob_chance": 0.45,
            "tax_rate": 0.13, "work_min": 200, "work_max": 700,
            "loan_interest": 0.20, "loan_penalty": 0.05,
            "business_tax": 0.05,
            "daily_min": 300, "daily_max": 500,
        },
    },
    "hardcore": {
        "label": "🔴 Хардкорная",
        "description": "Долгие кулдауны, низкие шансы, высокие налоги",
        "values": {
            "cooldown_work": 12 * _H, "cooldown_crime": 8 * _H, "cooldown_slut": 8 * _H,
            "cooldown_rob": 6 * _H, "cooldown_daily": 24 * _H,
            "crime_chance": 0.30, "rob_chance": 0.20,
            "tax_rate": 0.30, "work_min": 50, "work_max": 150,
            "loan_interest": 0.50, "loan_penalty": 0.10,
            "business_tax": 0.10,
            "daily_min": 300, "daily_max": 500,
        },
    },
}
DEFAULT_PRESET = "standard"
DEFAULT_SETTINGS = dict(PRESETS[DEFAULT_PRESET]["values"])
COOLDOWN_KEYS = ("work", "crime", "slut", "rob", "daily")

# ключ -> (тип, минимум, максимум, подпись для формы, подсказка)
SETTING_SPECS = {
    "cooldown_work": ("duration", 0, COOLDOWN_MAX, "Кулдаун !work", "4h, 30m, 90s или 0"),
    "cooldown_crime": ("duration", 0, COOLDOWN_MAX, "Кулдаун !crime", "2h, 30m или 0"),
    "cooldown_slut": ("duration", 0, COOLDOWN_MAX, "Кулдаун !slut", "3h, 30m или 0"),
    "cooldown_rob": ("duration", 0, COOLDOWN_MAX, "Кулдаун !rob", "3h, 30m или 0"),
    "cooldown_daily": ("duration", 0, COOLDOWN_MAX, "Кулдаун !daily", "24h или 0"),
    "crime_chance": ("rate", 0.0, 1.0, "Шанс успеха !crime и !slut", "0.60 или 60%"),
    "rob_chance": ("rate", 0.0, 1.0, "Шанс успеха !rob", "0.45 или 45%"),
    "tax_rate": ("rate", 0.0, 0.9, "Подоходный налог (НДФЛ)", "0.13 или 13%"),
    "business_tax": ("rate", 0.0, 0.5, "Налог на бизнес (от дохода)", "0.05 или 5%"),
    "loan_interest": ("rate", 0.0, 2.0, "Процент по кредиту", "0.20 или 20%"),
    "loan_penalty": ("rate", 0.0, 0.5, "Штраф просрочки (в сутки)", "0.05 или 5%"),
    "work_min": ("int", 0, 1_000_000, "Зарплата !work: минимум", "200"),
    "work_max": ("int", 0, 1_000_000, "Зарплата !work: максимум", "700"),
    "daily_min": ("int", 0, 1_000_000, "Бонус !daily: минимум", "300"),
    "daily_max": ("int", 0, 1_000_000, "Бонус !daily: максимум", "500"),
}
# Разделы формы «Кастомная» (в модальном окне Discord — не больше 5 полей)
SETTING_SECTIONS = {
    "chances": {"label": "🎲 Шансы", "title": "Шансы успеха",
                "keys": ["crime_chance", "rob_chance"]},
    "taxes": {"label": "💸 Налоги и кредит", "title": "Налоги и кредит",
              "keys": ["tax_rate", "business_tax", "loan_interest", "loan_penalty"]},
    "cooldowns": {"label": "⏱️ Кулдауны", "title": "Кулдауны команд",
                  "keys": ["cooldown_work", "cooldown_crime", "cooldown_slut",
                           "cooldown_rob", "cooldown_daily"]},
    "pay": {"label": "💰 Зарплата и бонус", "title": "Зарплата и бонус",
            "keys": ["work_min", "work_max", "daily_min", "daily_max"]},
}

# --- ИИ ----------------------------------------------------------------------
# По умолчанию используется Groq API (есть бесплатный тариф с лимитами).
# Можно сменить совместимый API, не переписывая код:
# AI_API_URL, AI_API_KEY, AI_MODEL. Если они не заданы — берутся GROQ_*.
DEFAULT_AI_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_AI_MODEL = "llama-3.3-70b-versatile"
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


class CooldownError(EconomyError):
    """Кулдаун ещё не прошёл (транзакция откатывается, кулдаун не расходуется)."""


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


def parse_amount(raw, available):
    """'all'/'все'/'всё' -> available; иначе положительное целое. Некорректный ввод -> None."""
    raw = str(raw or "").strip().lower()
    if raw in ("all", "max", "все", "всё"):
        return int(available)
    try:
        value = int(raw.replace(" ", "").replace("_", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_amount(raw, available):
    """'all'/'все'/'всё' -> available; иначе положительное целое. Некорректный ввод -> None."""
    raw = str(raw or "").strip().lower()
    if raw in ("all", "max", "все", "всё"):
        return int(available)
    try:
        value = int(raw.replace(" ", "").replace("_", ""))
    except ValueError:
        return None
    return value if value > 0 else None


async def safe_create_index(collection, keys, *, drop_on_conflict=False, **options):
    """Создаёт индекс и переживает IndexOptionsConflict (85) / IndexKeySpecsConflict (86):
    такой индекс уже есть, но с другими опциями (например, TTL из старой версии).
    drop_on_conflict=False — старый индекс остаётся как есть (для обычных индексов это безопасно);
    True — старый индекс пересоздаётся (нужно для уникальных индексов, иначе уникальности не будет)."""
    try:
        return await collection.create_index(keys, **options)
    except OperationFailure as e:
        if e.code not in (85, 86):
            raise
        name = getattr(collection, "name", "?")
        if not drop_on_conflict:
            log.warning("Индекс %s.%s уже существует с другими опциями — оставляю как есть", name, keys)
            return None
        log.warning("Индекс %s.%s конфликтует по опциям — пересоздаю", name, keys)
        info = await collection.index_information()
        want = [tuple(k) if isinstance(k, (list, tuple)) else (k, 1)
                for k in (keys if isinstance(keys, list) else [keys])]
        for index_name, spec in info.items():
            if index_name != "_id_" and [tuple(x) for x in spec.get("key", [])] == want:
                await collection.drop_index(index_name)
        return await collection.create_index(keys, **options)


def ukey(guild_id, user_id):
    """Фильтр документа игрока: баланс привязан к паре (сервер, пользователь)."""
    return {"guild_id": int(guild_id), "user_id": int(user_id)}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fmt_pct(value):
    return f"{float(value) * 100:g}%"


def fmt_cooldown(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "нет"
    parts = []
    for size, unit in ((86400, "д"), (3600, "ч"), (60, "мин"), (1, "с")):
        qty, seconds = divmod(seconds, size)
        if qty:
            parts.append(f"{qty} {unit}")
    return " ".join(parts)


def duration_input(seconds):
    """Секунды -> строка для формы: 4h / 30m / 90s / 0."""
    seconds = int(seconds)
    if seconds == 0:
        return "0"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


_DURATION_UNITS = {
    "": 60, "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "hr": 3600, "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "d": 86400, "day": 86400, "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
}


def parse_setting(key, raw):
    """Разбирает и проверяет значение настройки из формы. ValueError — с понятным текстом."""
    kind, lo, hi, _label, _hint = SETTING_SPECS[key]
    text = str(raw or "").strip().lower().replace(",", ".").replace(" ", "")
    if not text:
        raise ValueError("пустое значение")
    if kind == "rate":
        try:
            value = float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError:
            raise ValueError("ожидается число вроде 0.45 или 45%")
        if not lo <= value <= hi:
            raise ValueError(f"допустимо от {fmt_pct(lo)} до {fmt_pct(hi)}")
        return round(value, 4)
    if kind == "duration":
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zа-яё]*)", text)
        if not match or match.group(2) not in _DURATION_UNITS:
            raise ValueError("ожидается время вроде 4h, 30m, 90s, 1d или 0")
        value = int(float(match.group(1)) * _DURATION_UNITS[match.group(2)])
        if not lo <= value <= hi:
            raise ValueError(f"допустимо от 0 до {fmt_cooldown(hi)}")
        return value
    try:
        value = int(float(text))
    except ValueError:
        raise ValueError("ожидается целое число")
    if not lo <= value <= hi:
        raise ValueError(f"допустимо от {fmt(lo)} до {fmt(hi)}")
    return value


def format_setting(key, value):
    kind = SETTING_SPECS[key][0]
    if kind == "rate":
        return fmt_pct(value)
    if kind == "duration":
        return fmt_cooldown(value)
    return fmt(value)


def setting_input_default(key, value):
    kind = SETTING_SPECS[key][0]
    if kind == "rate":
        return fmt_pct(value)
    if kind == "duration":
        return duration_input(value)
    return str(int(value))


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


def business_tax(level, rate):
    return int(business_income(level) * rate)


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
    """Клиент OpenAI-совместимого API. Groq — бесплатный по умолчанию вариант
    (с ограничениями тарифа); провайдера можно заменить переменными окружения."""

    REPORT_PROMPT = (
        "Ты — финансовый аналитик. Проанализируй эти данные экономики и напиши "
        "краткий отчет (3-4 предложения) о состоянии рынка."
    )

    def __init__(self):
        self.api_key = os.getenv("AI_API_KEY") or os.getenv("GROQ_API_KEY")
        self.url = os.getenv("AI_API_URL", DEFAULT_AI_URL)
        model = os.getenv("AI_MODEL") or os.getenv("GROQ_MODEL", DEFAULT_AI_MODEL)
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
            raise AIError("Не задан AI_API_KEY или GROQ_API_KEY.")
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
            async with session.post(self.url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    log.error("Groq HTTP %s: %s", resp.status, body[:500])
                    raise AIError(f"ИИ-сервис вернул ошибку HTTP {resp.status}.")
                data = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("Groq недоступен")
            raise AIError("Не удалось связаться с ИИ-сервисом.")
        except json.JSONDecodeError:
            raise AIError("ИИ-сервис вернул некорректный ответ.")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError):
            raise AIError("ИИ-сервис вернул пустой ответ.")

    async def market_report(self, stats):
        data = json.dumps(stats, ensure_ascii=False)
        return await self.complete(self.REPORT_PROMPT, f"Данные экономики (JSON):\n{data}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# Бот
# =============================================================================
async def guild_only_check(ctx):
    """Баланс привязан к серверу, поэтому команды работают только на сервере."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


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
        self._settings_cache = {}
        self.add_check(guild_only_check)
        self.chart_lock = asyncio.Lock()
        self._web_runner = None
        self.chart_files = {
            "all": "/tmp/cb_economy_all.png",
            "24h": "/tmp/cb_economy_24h.png",
        }

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
        self.casino_stats = self.db.casino_stats

        # Старые документы (users._id = user_id) переносим ДО создания уникальных индексов.
        await self.migrate_legacy()
        await safe_create_index(self.history, "created_at")
        await safe_create_index(self.tx, "created_at")
        await safe_create_index(self.tx, [("guild_id", 1), ("created_at", 1)])
        await safe_create_index(self.db_users, [("guild_id", 1), ("user_id", 1)],
                                unique=True, drop_on_conflict=True)
        await safe_create_index(self.db_users, [("guild_id", 1), ("cash", -1)])
        await safe_create_index(self.businesses, [("guild_id", 1), ("user_id", 1)],
                                unique=True, drop_on_conflict=True)
        await safe_create_index(self.casino_stats, [("guild_id", 1), ("user_id", 1)],
                                unique=True, drop_on_conflict=True)
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
        await self.add_cog(SettingsCog(self))
        if not self.ai.enabled:
            log.info("GROQ_API_KEY не задан — ИИ-функции отключены.")
        try:
            synced = await self.tree.sync()
            log.info("Синхронизировано slash-команд: %s", len(synced))
        except Exception:
            log.exception("Slash sync failed")
        self.business_loop.start()
        self.loan_loop.start()
        self.snapshot_loop.start()
        self.daily_report_loop.start()

    async def close(self):
        for loop in (self.business_loop, self.loan_loop, self.snapshot_loop, self.daily_report_loop):
            if loop.is_running():
                loop.cancel()
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

    # --- Миграция со старой схемы -------------------------------------------
    async def migrate_legacy(self):
        """Старые документы (без user_id; _id = id игрока) принадлежали единой глобальной
        экономике. Если задана переменная LEGACY_GUILD_ID — они переносятся на этот сервер.
        Иначе они не удаляются, а переносятся в архивные коллекции <имя>_legacy
        (позже их можно импортировать, задав LEGACY_GUILD_ID и перезапустив бота)."""
        raw = (os.getenv("LEGACY_GUILD_ID") or "").strip()
        legacy_gid = int(raw) if raw.isdigit() else None
        for name, coll in (("users", self.db_users), ("businesses", self.businesses),
                           ("casino_stats", self.casino_stats)):
            archive = self.db[f"{name}_legacy"]
            moved = 0
            async for doc in coll.find({"user_id": {"$exists": False}}):
                if legacy_gid is None:
                    try:
                        await archive.insert_one(doc)
                    except DuplicateKeyError:
                        pass
                    await coll.delete_one({"_id": doc["_id"]})
                else:
                    await self._import_legacy_doc(coll, doc, legacy_gid)
                    await coll.delete_one({"_id": doc["_id"]})
                moved += 1
            if legacy_gid is not None:
                async for doc in archive.find({}):
                    if await self._import_legacy_doc(coll, doc, legacy_gid):
                        moved += 1
                    await archive.delete_one({"_id": doc["_id"]})
            if moved:
                if legacy_gid is None:
                    log.warning("Миграция: %s старых документов %s перенесено в %s_legacy. "
                                "Задайте LEGACY_GUILD_ID, чтобы импортировать их на нужный сервер.",
                                moved, name, name)
                else:
                    log.info("Миграция: %s документов %s перенесено на сервер %s", moved, name, legacy_gid)

    @staticmethod
    async def _import_legacy_doc(coll, doc, guild_id):
        new = {k: v for k, v in doc.items() if k != "_id"}
        new["guild_id"] = int(guild_id)
        new["user_id"] = int(doc["_id"])
        try:
            await coll.insert_one(new)
            return True
        except DuplicateKeyError:
            return False  # у игрока на этом сервере уже есть новый документ — старый не затираем

    # --- Настройки сервера ---------------------------------------------------
    def invalidate_settings(self, guild_id):
        self._settings_cache.pop(int(guild_id), None)

    async def get_settings(self, guild_id) -> dict:
        """Настройки экономики сервера. Если в guild_settings пусто — пресет «Стандартная»."""
        gid = int(guild_id)
        cached = self._settings_cache.get(gid)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        doc = await self.settings.find_one({"_id": gid}) or {}
        eco = doc.get("eco") or {}
        cfg = dict(DEFAULT_SETTINGS)
        legacy_used = False
        for key in COOLDOWN_KEYS:  # старый формат: cooldown_<команда> в корне документа
            legacy = doc.get(f"cooldown_{key}")
            if _is_number(legacy) and legacy >= 0:
                cfg[f"cooldown_{key}"] = legacy
                legacy_used = True
        for key, value in eco.items():
            if key in DEFAULT_SETTINGS and _is_number(value):
                cfg[key] = value
        for key, (kind, *_rest) in SETTING_SPECS.items():
            cfg[key] = float(cfg[key]) if kind == "rate" else int(cfg[key])
        preset = eco.get("preset")
        if preset not in PRESETS and preset != "custom":
            preset = "custom" if legacy_used else DEFAULT_PRESET
        cfg["preset"] = preset
        self._settings_cache[gid] = (time.monotonic() + 30, cfg)
        return dict(cfg)

    async def get_cooldown(self, guild_id, key: str) -> int:
        return int((await self.get_settings(guild_id))[f"cooldown_{key}"])

    # --- Работа с БД ---------------------------------------------------------
    async def ensure_user(self, guild_id: int, user_id: int, *, session=None):
        """Находит или создаёт документ игрока на сервере (guild_id, user_id)."""
        flt = ukey(guild_id, user_id)
        try:
            await self.db_users.update_one(
                flt,
                {"$setOnInsert": {"cash": 0, "bank": 0, "debt": 0,
                                  "last_work": None, "last_crime": None, "last_slut": None,
                                  "last_rob": None, "last_daily": None, "created_at": now()}},
                upsert=True, session=session,
            )
        except DuplicateKeyError:
            pass  # параллельный запрос уже создал документ
        return await self.db_users.find_one(flt, session=session)

    async def get_balance(self, guild_id: int, user_id: int):
        return await self.ensure_user(guild_id, user_id)

    async def change_cash(self, guild_id: int, user_id: int, delta: int, *, session=None):
        """Атомарное изменение наличных. Отрицательное изменение не уводит cash в минус."""
        delta = int(delta)
        await self.ensure_user(guild_id, user_id, session=session)
        flt = ukey(guild_id, user_id)
        if delta < 0:
            flt["cash"] = {"$gte": -delta}
        result = await self.db_users.update_one(
            flt, {"$inc": {"cash": delta}, "$set": {"updated_at": now()}}, session=session)
        return result.modified_count == 1

    async def change_bank(self, guild_id: int, user_id: int, delta: int, *, session=None):
        delta = int(delta)
        await self.ensure_user(guild_id, user_id, session=session)
        flt = ukey(guild_id, user_id)
        if delta < 0:
            flt["bank"] = {"$gte": -delta}
        result = await self.db_users.update_one(
            flt, {"$inc": {"bank": delta}, "$set": {"updated_at": now()}}, session=session)
        return result.modified_count == 1

    async def journal(self, source, destination, amount, reason, meta=None, *, session=None, guild_id=None):
        doc = {
            "source": str(source), "destination": str(destination),
            "amount": int(amount), "reason": reason,
            "meta": meta or {}, "created_at": now(),
        }
        if guild_id is not None:
            doc["guild_id"] = int(guild_id)
        await self.tx.insert_one(doc, session=session)

    async def transfer_players(self, guild_id: int, sender_id: int, recipient_id: int, amount: int):
        amount = int(amount)
        if amount <= 0 or sender_id == recipient_id:
            return False, "Сумма должна быть положительной, а получатель — другим игроком."
        await self.ensure_user(guild_id, sender_id)
        await self.ensure_user(guild_id, recipient_id)
        try:
            # MongoDB transactions require a replica set/sharded cluster (Atlas supports this).
            async with await self.mongo.start_session() as session:
                async with session.start_transaction():
                    sender = await self.db_users.find_one(ukey(guild_id, sender_id), session=session)
                    if int(sender.get("debt", 0)) > 0:
                        raise EconomyError("У тебя есть долг. Погаси его перед переводами.")
                    if int(sender.get("cash", 0)) < amount:
                        raise EconomyError("Недостаточно наличных.")
                    debited = await self.db_users.update_one(
                        {**ukey(guild_id, sender_id), "cash": {"$gte": amount}, "debt": 0},
                        {"$inc": {"cash": -amount}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if debited.modified_count != 1:
                        raise EconomyError("Перевод отклонён: проверь баланс и долг.")
                    await self.db_users.update_one(
                        ukey(guild_id, recipient_id),
                        {"$inc": {"cash": amount}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    await self.journal(f"user_{sender_id}", f"user_{recipient_id}", amount,
                                       "Перевод между игроками",
                                       {"type": "player_transfer"}, session=session, guild_id=guild_id)
        except EconomyError as e:
            return False, str(e)
        return True, "Перевод выполнен."

    async def claim_cooldown(self, guild_id: int, user_id: int, key: str, cooldown: int, *, session) -> bool:
        """Атомарно «занимает» кулдаун: апдейт срабатывает, только если last_<key> пуст
        или старше cooldown секунд. Две одновременные команды не пройдут обе."""
        field = f"last_{key}"
        current = now()
        threshold = current - timedelta(seconds=int(cooldown))
        result = await self.db_users.update_one(
            {**ukey(guild_id, user_id), "$or": [{field: None}, {field: {"$lte": threshold}}]},
            {"$set": {field: current, "updated_at": current}},
            session=session,
        )
        return result.matched_count == 1

    async def cooldown_left(self, guild_id: int, user_id: int, key: str, cooldown: int) -> float:
        doc = await self.db_users.find_one(ukey(guild_id, user_id), {f"last_{key}": 1}) or {}
        last = as_utc(doc.get(f"last_{key}"))
        if last is None:
            return 0
        return max(0, cooldown - (now() - last).total_seconds())

    async def fund_balance(self, name="state_budget"):
        doc = await self.funds.find_one({"_id": name})
        return int((doc or {}).get("balance", 0))

    async def add_budget(self, amount: int, reason: str, *, session=None, source="taxpayer", guild_id=None):
        """Госбюджет — глобальный. guild_id нужен только для журнала (статистика по серверу)."""
        amount = int(amount)
        if amount <= 0:
            return
        await self.funds.update_one(
            {"_id": "state_budget"},
            {"$inc": {"balance": amount}, "$set": {"updated_at": now()},
             "$setOnInsert": {"name": "Государственный бюджет", "created_at": now()}},
            upsert=True, session=session,
        )
        await self.journal(source, "fund_state_budget", amount, reason, session=session, guild_id=guild_id)

    async def _pay_earning_tx(self, session, guild_id, user_id, gross, tax, net, reason, account, tax_rate):
        await self.db_users.update_one(
            ukey(guild_id, user_id),
            {"$inc": {account: net}, "$set": {"updated_at": now()}},
            session=session,
        )
        await self.add_budget(tax, f"НДФЛ {fmt_pct(tax_rate)}: {reason}", session=session,
                              guild_id=guild_id)
        await self.journal("economy", f"user_{user_id}", net, reason,
                           {"gross": gross, "tax": tax, "tax_rate": tax_rate},
                           session=session, guild_id=guild_id)

    async def pay_earning(self, guild_id: int, user_id: int, gross: int, reason: str, *,
                          tax_rate: float, account="cash", session=None):
        """Начисляет доход за вычетом НДФЛ (ставка — из настроек сервера). Если передана session —
        работает внутри уже открытой транзакции вызывающего (вложенные транзакции запрещены)."""
        gross = max(0, int(gross))
        tax = int(gross * float(tax_rate))
        net = gross - tax
        await self.ensure_user(guild_id, user_id, session=session)
        if session is not None:
            await self._pay_earning_tx(session, guild_id, user_id, gross, tax, net, reason, account, tax_rate)
        else:
            async with await self.mongo.start_session() as own_session:
                async with own_session.start_transaction():
                    await self._pay_earning_tx(own_session, guild_id, user_id, gross, tax, net,
                                               reason, account, tax_rate)
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

    async def stats(self, guild_id=None):
        """Резерв ЦБ, госбюджет и фонды — глобальные. Игроки, долги и обороты за 24 ч —
        по серверу, если передан guild_id (иначе по всем серверам)."""
        reserve = await self.reserve()
        budget = await self.fund_balance("state_budget")
        fund_rows = await self.funds.aggregate([{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]).to_list(1)
        funds_total = int(fund_rows[0]["total"]) if fund_rows else 0
        scope = {"guild_id": int(guild_id)} if guild_id is not None else {"guild_id": {"$exists": True}}
        players_rows = await self.db_users.aggregate([
            {"$match": scope},
            {"$group": {"_id": None, "cash": {"$sum": "$cash"}, "bank": {"$sum": "$bank"}, "debt": {"$sum": "$debt"}}},
        ]).to_list(1)
        p = players_rows[0] if players_rows else {}
        players = int(p.get("cash", 0)) + int(p.get("bank", 0))
        player_debt = int(p.get("debt", 0))
        central = await self.economy.find_one({"_id": "central_bank"}) or {}
        since = now() - timedelta(hours=24)
        tx_scope = {"guild_id": int(guild_id)} if guild_id is not None else {}
        incoming = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "destination": {"$regex": "^user_"}, **tx_scope}},
            {"$group": {"_id": None, "n": {"$sum": "$amount"}}},
        ]).to_list(1)
        outgoing = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "source": {"$regex": "^user_"}, **tx_scope}},
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
            gid, uid = business.get("guild_id"), business.get("user_id")
            if gid is None or uid is None:
                continue
            gid, uid = int(gid), int(uid)
            level = max(1, int(business.get("level", 1)))
            try:
                cfg = await self.get_settings(gid)
                income = business_income(level)
                tax = business_tax(level, cfg["business_tax"])
                async with await self.mongo.start_session() as session:
                    async with session.start_transaction():
                        await self.ensure_user(gid, uid, session=session)
                        # Налог на имущество списывается с наличных. Нет наличных — бизнес замораживается.
                        debit = await self.db_users.update_one(
                            {**ukey(gid, uid), "cash": {"$gte": tax}},
                            {"$inc": {"cash": -tax, "bank": income}, "$set": {"updated_at": current}},
                            session=session,
                        )
                        if debit.modified_count != 1:
                            await self.businesses.update_one(
                                ukey(gid, uid),
                                {"$set": {"frozen": True, "last_checked": current}},
                                session=session,
                            )
                            continue
                        await self.add_budget(tax, f"Налог на имущество бизнеса {fmt_pct(cfg['business_tax'])}",
                                              session=session, guild_id=gid)
                        await self.businesses.update_one(
                            ukey(gid, uid),
                            {"$set": {"last_payout": current, "last_checked": current, "frozen": False}},
                            session=session,
                        )
                        await self.journal(f"business_{uid}", f"user_{uid}", income,
                                           "Доход бизнеса", {"property_tax": tax, "level": level},
                                           session=session, guild_id=gid)
                log.info("Business payout guild=%s uid=%s level=%s income=%s tax=%s", gid, uid, level, income, tax)
            except Exception:
                log.exception("Business payout failed for guild=%s uid=%s", gid, uid)

    @business_loop.before_loop
    async def before_business_loop(self):
        await self.wait_until_ready()

    @tasks.loop(hours=1)
    async def loan_loop(self):
        """Просрочка кредита: + loan_penalty (из настроек сервера) к долгу за каждые полные сутки."""
        current = now()
        async for user in self.db_users.find({"debt": {"$gt": 0}}):
            gid, uid = user.get("guild_id"), user.get("user_id")
            if gid is None or uid is None:
                continue
            gid, uid = int(gid), int(uid)
            try:
                due = as_utc(user.get("loan_due"))
                if due is None:
                    # Долг без срока: срок отсчитывается с первого обнаружения.
                    await self.db_users.update_one(
                        {**ukey(gid, uid), "debt": {"$gt": 0}, "loan_due": None},
                        {"$set": {"loan_due": current + timedelta(seconds=LOAN_TERM)}},
                    )
                    continue
                if current <= due:
                    continue
                last = as_utc(user.get("loan_penalty_at")) or due
                periods = min(int((current - last).total_seconds() // 86400), 30)
                if periods < 1:
                    continue
                rate = (await self.get_settings(gid))["loan_penalty"]
                debt = int(user["debt"])
                new_debt = debt
                for _ in range(periods):
                    new_debt += int(new_debt * rate)
                # Даже при нулевом штрафе сдвигаем отметку, чтобы потом не начислить задним числом.
                result = await self.db_users.update_one(
                    {**ukey(gid, uid), "debt": debt},
                    {"$set": {"debt": new_debt, "loan_penalty_at": last + timedelta(days=periods),
                              "updated_at": current}},
                )
                if result.modified_count == 1 and new_debt != debt:
                    log.info("Loan overdue penalty guild=%s uid=%s debt %s -> %s (%s дн.)",
                             gid, uid, debt, new_debt, periods)
            except Exception:
                log.exception("Loan penalty failed for guild=%s uid=%s", gid, uid)

    @loan_loop.before_loop
    async def before_loan_loop(self):
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
                stats = await self.stats(int(config["_id"]))
                report_channel_id = config.get("report_channel_id")
                if report_channel_id:
                    channel = await self._resolve_channel(guild, report_channel_id)
                    if channel is not None:
                        await safe_send(
                            channel,
                            "📊 **Ежедневный экономический отчёт**\n"
                            f"🏦 Резерв ЦБ: `{fmt(stats['reserve'])}`\n"
                            f"💰 Госбюджет: `{fmt(stats['budget'])}`\n"
                            f"👥 Средства игроков сервера: `{fmt(stats['players'])}`\n"
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
CRIME_ACTIONS = {
    "crime": {
        "title": "🕵️ Кража",
        "reward": CRIME_REWARD,
        "win": [
            "Ты тихо вскрыл склад и ушёл незамеченным.",
            "Охрана смотрела футбол — ты вынес всё, что плохо лежало.",
            "Карманная кража в метро прошла идеально.",
        ],
        "lose": [
            "Тебя поймала охрана и вызвала полицию.",
            "Сработала сигнализация — пришлось бежать, но тебя опознали.",
            "Жертва оказалась переодетым полицейским.",
        ],
        "wait": "Полиция ещё ищет тебя после прошлого дела.",
        "reason_win": "Кража: добыча",
        "reason_fine": "Штраф за неудачную кражу",
    },
    "slut": {
        "title": "💋 Ночная подработка",
        "reward": SLUT_REWARD,
        "win": [
            "Клиент оказался щедрым и оставил хорошие чаевые.",
            "Вечер удался — смена принесла отличный заработок.",
            "VIP-гость заплатил вдвое больше обычного.",
        ],
        "lose": [
            "Нагрянула облава — пришлось платить штраф.",
            "Клиент сбежал, не заплатив, а тебя ещё и оштрафовали.",
            "Конкуренты вызвали проверку, и ты попал под штраф.",
        ],
        "wait": "Ты ещё не отдохнул после прошлой смены.",
        "reason_win": "Ночная подработка: доход",
        "reason_fine": "Штраф за неудачную ночную подработку",
    },
}


class EconomyCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Команды экономики")
    async def help_command(self, ctx):
        await safe_send(
            ctx,
            "**Экономика сервера** (баланс у каждого сервера свой)\n"
            "`!balance` — баланс; `!pay @user сумма` — перевод\n"
            "`!work`, `!crime`, `!slut`, `!daily` — заработок; `!rob @user` — ограбить игрока; `!top` — топ-10\n"
            "`!deposit сумма|all`, `!withdraw сумма|all` — положить в банк / снять наличные\n"
            "`!business buy` — купить бизнес; `!business status` — статус\n"
            "`!business upgrade` — улучшить бизнес (оплата с банковского счёта)\n"
            "`!loan take сумма`; `!loan pay сумма` — кредит и погашение\n"
            "`!economy` — статистика сервера; `!cb` — Центральный банк (общий для всех серверов)\n"
            "**Казино** (ставки из наличных):\n"
            f"`!slot ставка`, `!bj ставка`, `!rr ставка` (от {fmt(CASINO_MIN_BET)} до {fmt(CASINO_MAX_BET)}, можно `all`)\n"
            "`!casinostats [@user]` — статистика казино\n"
            "**Админ:**\n"
            "`/settings` — панель настроек экономики (пресеты и ручная настройка)\n"
            "`/graphs` или `!graphs` — графики\n"
            "`/setreportchannel`, `/setchartchannel` — каналы отчётов и графиков\n"
            "`!setcooldown work|crime|slut|daily|rob секунды` — кулдаун команды\n"
            "`!ai_report` — ИИ-отчёт; `!ai_toggle` — вкл/выкл ИИ на сервере"
        )

    @commands.hybrid_command(name="balance", description="Показать баланс")
    async def balance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        data = await self.bot.get_balance(ctx.guild.id, member.id)
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
        ok, message = await self.bot.transfer_players(ctx.guild.id, ctx.author.id, member.id, amount)
        await safe_send(ctx, ("✅ " if ok else "❌ ") + message)

    @commands.hybrid_command(name="work", description="Поработать и получить зарплату")
    async def work(self, ctx):
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        cooldown = cfg["cooldown_work"]
        low, high = sorted((cfg["work_min"], cfg["work_max"]))
        gross = random.randint(low, high)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    # Атомарная проверка кулдауна ($or в фильтре): гонка двух команд невозможна.
                    if not await self.bot.claim_cooldown(gid, uid, "work", cooldown, session=session):
                        raise CooldownError("cooldown")
                    _, tax, net = await self.bot.pay_earning(
                        gid, uid, gross, "Зарплата за работу",
                        tax_rate=cfg["tax_rate"], session=session)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "work", cooldown)
            return await safe_send(
                ctx, f"⏳ Ты устал. Следующая работа через **{fmt_duration(remaining)}**.")
        text = f"🛠️ Ты заработал **{fmt(gross)}** монет.\n"
        if tax > 0:
            text += f"НДФЛ ({fmt_pct(cfg['tax_rate'])}): **{fmt(tax)}** в госбюджет.\n"
        text += f"На руки: **{fmt(net)}** монет."
        await safe_send(ctx, text)

    async def _criminal_action(self, ctx, key: str):
        """Общая логика !crime и !slut. Успех (шанс crime_chance из настроек сервера): награда из
        резерва ЦБ без НДФЛ. Провал: штраф min(20% баланса, 500) в госбюджет. Любая ошибка
        (в т.ч. нехватка резерва) откатывает транзакцию вместе с кулдауном."""
        cfg_action = CRIME_ACTIONS[key]
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        cooldown = cfg[f"cooldown_{key}"]
        success = rng.random() < cfg["crime_chance"]
        reward = rng.randint(*cfg_action["reward"])
        fine = 0
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.claim_cooldown(gid, uid, key, cooldown, session=session):
                        raise CooldownError("cooldown")
                    if success:
                        if not await self.bot.change_reserve(-reward, session=session):
                            raise EconomyError(
                                "В резерве ЦБ недостаточно средств. Попроси админа сделать эмиссию.")
                        await self.bot.change_cash(gid, uid, reward, session=session)
                        await self.bot.journal("central_bank", f"user_{uid}", reward,
                                               cfg_action["reason_win"], {"type": key},
                                               session=session, guild_id=gid)
                    else:
                        user = await self.bot.db_users.find_one(ukey(gid, uid), session=session) or {}
                        cash, bank = int(user.get("cash", 0)), int(user.get("bank", 0))
                        fine = min(int((cash + bank) * CRIME_FINE_RATE), CRIME_FINE_MAX)
                        if fine > 0:
                            from_cash = min(fine, cash)
                            from_bank = fine - from_cash
                            debited = await self.bot.db_users.update_one(
                                {**ukey(gid, uid), "cash": {"$gte": from_cash}, "bank": {"$gte": from_bank}},
                                {"$inc": {"cash": -from_cash, "bank": -from_bank},
                                 "$set": {"updated_at": now()}},
                                session=session,
                            )
                            if debited.modified_count != 1:
                                raise EconomyError("Баланс изменился во время операции. Попробуй ещё раз.")
                            await self.bot.add_budget(fine, cfg_action["reason_fine"], session=session,
                                                      source=f"user_{uid}", guild_id=gid)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, key, cooldown)
            return await safe_send(
                ctx, f"⏳ {cfg_action['wait']} Попробуй через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")

        if success:
            await safe_send(
                ctx,
                f"{cfg_action['title']}\n{rng.choice(cfg_action['win'])}\n"
                f"💰 Добыча: **{fmt(reward)}** монет (без налога)."
            )
        elif fine > 0:
            await safe_send(
                ctx,
                f"{cfg_action['title']}\n{rng.choice(cfg_action['lose'])}\n"
                f"🚔 Штраф: **{fmt(fine)}** монет уходит в госбюджет."
            )
        else:
            await safe_send(
                ctx,
                f"{cfg_action['title']}\n{rng.choice(cfg_action['lose'])}\n"
                "🚔 У тебя нечего отбирать — штраф 0."
            )

    @commands.hybrid_command(name="crime", description="Кража: награда из резерва ЦБ, шанс зависит от настроек сервера")
    async def crime(self, ctx):
        await self._criminal_action(ctx, "crime")

    @commands.hybrid_command(name="slut", description="Ночная подработка: награда из резерва ЦБ, шанс зависит от настроек сервера")
    async def slut(self, ctx):
        await self._criminal_action(ctx, "slut")

    @commands.hybrid_command(name="daily", description="Ежедневный бонус из резерва ЦБ")
    async def daily(self, ctx):
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        cooldown = cfg["cooldown_daily"]
        low, high = sorted((cfg["daily_min"], cfg["daily_max"]))
        reward = rng.randint(low, high)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.claim_cooldown(gid, uid, "daily", cooldown, session=session):
                        raise CooldownError("cooldown")
                    if reward > 0:
                        if not await self.bot.change_reserve(-reward, session=session):
                            raise EconomyError("В резерве ЦБ недостаточно средств. Попроси админа сделать эмиссию.")
                        await self.bot.change_cash(gid, uid, reward, session=session)
                        await self.bot.journal("central_bank", f"user_{uid}", reward,
                                               "Ежедневный бонус", {"type": "daily"},
                                               session=session, guild_id=gid)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "daily", cooldown)
            return await safe_send(ctx, f"⏳ Бонус уже получен. Следующий через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"🎁 Ежедневный бонус: **{fmt(reward)}** монет (без налога).")

    @commands.hybrid_command(name="rob", description="Ограбить игрока: при провале — штраф в пользу жертвы")
    @app_commands.describe(member="Кого грабим")
    async def rob(self, ctx, member: discord.Member):
        gid, uid, tid = ctx.guild.id, ctx.author.id, member.id
        if member.bot or tid == uid:
            return await safe_send(ctx, "❌ Нельзя грабить себя или ботов.")
        await self.bot.ensure_user(gid, uid)
        await self.bot.ensure_user(gid, tid)
        cfg = await self.bot.get_settings(gid)
        cooldown = cfg["cooldown_rob"]
        success = rng.random() < cfg["rob_chance"]
        loot_rate = rng.uniform(*ROB_LOOT_RATE)
        amount = 0
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.claim_cooldown(gid, uid, "rob", cooldown, session=session):
                        raise CooldownError("cooldown")
                    victim = await self.bot.db_users.find_one(ukey(gid, tid), session=session) or {}
                    vcash = int(victim.get("cash", 0))
                    if vcash < ROB_MIN_TARGET_CASH:
                        raise EconomyError(
                            f"У {member.display_name} меньше {fmt(ROB_MIN_TARGET_CASH)} наличных — не стоит рисковать.")
                    robbed_at = as_utc(victim.get("last_robbed"))
                    if robbed_at and (now() - robbed_at).total_seconds() < ROB_VICTIM_PROTECTION:
                        left = ROB_VICTIM_PROTECTION - (now() - robbed_at).total_seconds()
                        raise EconomyError(
                            f"{member.display_name} недавно ограбили. Защита ещё {fmt_duration(left)}.")
                    if success:
                        amount = max(1, min(int(vcash * loot_rate), ROB_MAX_LOOT))
                        taken = await self.bot.db_users.update_one(
                            {**ukey(gid, tid), "cash": {"$gte": amount}},
                            {"$inc": {"cash": -amount}, "$set": {"last_robbed": now(), "updated_at": now()}},
                            session=session,
                        )
                        if taken.modified_count != 1:
                            raise EconomyError("Баланс жертвы изменился. Попробуй ещё раз.")
                        await self.bot.change_cash(gid, uid, amount, session=session)
                        await self.bot.journal(f"user_{tid}", f"user_{uid}", amount,
                                               "Ограбление", {"type": "rob"},
                                               session=session, guild_id=gid)
                    else:
                        thief = await self.bot.db_users.find_one(ukey(gid, uid), session=session) or {}
                        amount = min(int(int(thief.get("cash", 0)) * ROB_FINE_RATE), ROB_FINE_MAX)
                        if amount > 0:
                            if not await self.bot.change_cash(gid, uid, -amount, session=session):
                                raise EconomyError("Баланс изменился. Попробуй ещё раз.")
                            await self.bot.change_cash(gid, tid, amount, session=session)
                            await self.bot.journal(f"user_{uid}", f"user_{tid}", amount,
                                                   "Штраф за неудачное ограбление", {"type": "rob_fail"},
                                                   session=session, guild_id=gid)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "rob", cooldown)
            return await safe_send(ctx, f"⏳ Копы ещё рядом. Попробуй через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        if success:
            await safe_send(ctx, f"🥷 Ты ограбил {member.mention} на **{fmt(amount)}** монет (без налога).")
        elif amount > 0:
            await safe_send(ctx, f"🚔 Тебя поймали! Ты выплатил {member.mention} компенсацию: **{fmt(amount)}** монет.")
        else:
            await safe_send(ctx, "🚔 Тебя поймали, но с тебя нечего взять — штраф 0.")

    @commands.hybrid_command(name="setcooldown", description="Задать кулдаун команды на этом сервере (админ)")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    @app_commands.describe(command="Команда: work, crime, slut, daily или rob", seconds="Кулдаун в секундах (0 — без кулдауна)")
    async def setcooldown(self, ctx, command: Literal["work", "crime", "slut", "daily", "rob"], seconds: int):
        if seconds < 0 or seconds > COOLDOWN_MAX:
            return await safe_send(ctx, f"❌ Кулдаун должен быть от 0 до {fmt(COOLDOWN_MAX)} секунд.")
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {f"eco.cooldown_{command}": int(seconds), "eco.preset": "custom", "updated_at": now()}},
            upsert=True,
        )
        self.bot.invalidate_settings(ctx.guild.id)
        text = "без кулдауна" if seconds == 0 else fmt_duration(seconds)
        await safe_send(ctx, f"✅ Кулдаун `{command}` теперь: **{text}** (пресет: ⚙️ Кастомная).")

    async def _move_money(self, ctx, raw_amount: str, *, to_bank: bool):
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        src = "cash" if to_bank else "bank"
        amount = parse_amount(raw_amount, int(user.get(src, 0)))
        if not amount:
            return await safe_send(ctx, "❌ Укажи положительную сумму или `all`.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    moved = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), src: {"$gte": amount}},
                        {"$inc": {"cash": -amount if to_bank else amount,
                                  "bank": amount if to_bank else -amount},
                         "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if moved.modified_count != 1:
                        raise EconomyError("Недостаточно средств на счёте.")
                    await self.bot.journal(
                        f"user_{uid}_{src}", f"user_{uid}_{'bank' if to_bank else 'cash'}", amount,
                        "Пополнение банковского счёта" if to_bank else "Снятие наличных",
                        {"type": "deposit" if to_bank else "withdraw"}, session=session, guild_id=gid)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"{'🏦 В банк' if to_bank else '💵 Снято'}: **{fmt(amount)}** монет.")

    @commands.hybrid_command(name="deposit", aliases=["dep"], description="Положить наличные в банк")
    @app_commands.describe(amount="Сумма или all")
    async def deposit(self, ctx, amount: str):
        await self._move_money(ctx, amount, to_bank=True)

    @commands.hybrid_command(name="withdraw", aliases=["with"], description="Снять деньги с банковского счёта")
    @app_commands.describe(amount="Сумма или all")
    async def withdraw(self, ctx, amount: str):
        await self._move_money(ctx, amount, to_bank=False)

    @commands.hybrid_command(name="top", description="Топ-10 богатейших игроков сервера")
    async def top(self, ctx):
        rows = await self.bot.db_users.aggregate([
            {"$match": {"guild_id": ctx.guild.id}},
            {"$addFields": {"net": {"$subtract": [
                {"$add": [{"$ifNull": ["$cash", 0]}, {"$ifNull": ["$bank", 0]}]},
                {"$ifNull": ["$debt", 0]}]}}},
            {"$sort": {"net": -1}},
            {"$limit": 10},
        ]).to_list(10)
        lines = []
        for i, row in enumerate(rows, 1):
            uid = int(row["user_id"])
            member = ctx.guild.get_member(uid)
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
    def _business_text(doc, tax_rate):
        level = int(doc.get("level", 1))
        state = "заморожен" if doc.get("frozen") else "работает"
        text = (
            f"🏢 Бизнес: уровень `{level}`/{BUSINESS_MAX_LEVEL}, статус: **{state}**.\n"
            f"Доход: `{fmt(business_income(level))}`/час; "
            f"налог: `{fmt(business_tax(level, tax_rate))}`/час из наличных ({fmt_pct(tax_rate)} дохода)."
        )
        if level < BUSINESS_MAX_LEVEL:
            text += (f"\nУлучшение до уровня {level + 1}: `{fmt(upgrade_cost(level))}` "
                     f"с банковского счёта (`!business upgrade`).")
        else:
            text += "\nДостигнут максимальный уровень."
        return text

    @commands.hybrid_group(name="business", description="Управление бизнесом", invoke_without_command=True)
    async def business(self, ctx):
        await self._show_business(ctx)

    @business.command(name="buy", description="Купить бизнес")
    async def business_buy(self, ctx):
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        if int(user.get("debt", 0)) > 0:
            return await safe_send(ctx, "❌ Сначала полностью погаси долг, затем покупай бизнес.")
        if await self.bot.businesses.find_one(ukey(gid, uid)):
            return await safe_send(ctx, "❌ У тебя уже есть бизнес.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    result = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "cash": {"$gte": BUSINESS_COST}, "debt": 0},
                        {"$inc": {"cash": -BUSINESS_COST}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if result.modified_count != 1:
                        raise EconomyError(f"Нужно {fmt(BUSINESS_COST)} наличными и отсутствие долга.")
                    try:
                        await self.bot.businesses.insert_one({
                            **ukey(gid, uid), "level": 1, "last_payout": now(),
                            "frozen": False, "created_at": now(),
                        }, session=session)
                    except DuplicateKeyError:
                        raise EconomyError("У тебя уже есть бизнес.")
                    await self.bot.journal(f"user_{uid}", "business_market", BUSINESS_COST,
                                           "Покупка бизнеса", session=session, guild_id=gid)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"✅ Бизнес куплен за **{fmt(BUSINESS_COST)}**. Первый доход начислится примерно через час.")

    @business.command(name="status", description="Статус бизнеса")
    async def business_status(self, ctx):
        await self._show_business(ctx)

    async def _show_business(self, ctx):
        gid = ctx.guild.id
        doc = await self.bot.businesses.find_one(ukey(gid, ctx.author.id))
        if not doc:
            return await safe_send(ctx, "У тебя пока нет бизнеса. Используй `!business buy`.")
        cfg = await self.bot.get_settings(gid)
        await safe_send(ctx, self._business_text(doc, cfg["business_tax"]))

    @business.command(name="upgrade", description="Улучшить бизнес (+500 монет/час за уровень)")
    async def business_upgrade(self, ctx):
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    doc = await self.bot.businesses.find_one(ukey(gid, uid), session=session)
                    if not doc:
                        raise EconomyError("У тебя пока нет бизнеса. Используй `!business buy`.")
                    level = int(doc.get("level", 1))
                    if level >= BUSINESS_MAX_LEVEL:
                        raise EconomyError("Бизнес уже достиг максимального уровня.")
                    cost = upgrade_cost(level)
                    if not await self.bot.change_bank(gid, uid, -cost, session=session):
                        raise EconomyError(f"Для улучшения нужно `{fmt(cost)}` на банковском счёте.")
                    upgraded = await self.bot.businesses.update_one(
                        {**ukey(gid, uid), "level": level},
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
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        new_level = level + 1
        await safe_send(
            ctx,
            f"⬆️ Бизнес улучшен до уровня **{new_level}** за **{fmt(cost)}**.\n"
            f"Доход: `{fmt(business_income(new_level))}`/час, "
            f"налог: `{fmt(business_tax(new_level, cfg['business_tax']))}`/час."
        )

    # --- Кредиты -------------------------------------------------------------
    @commands.hybrid_group(name="loan", description="Кредиты", invoke_without_command=True)
    async def loan(self, ctx):
        gid = ctx.guild.id
        data = await self.bot.get_balance(gid, ctx.author.id)
        cfg = await self.bot.get_settings(gid)
        text = f"💳 Твой долг: `{fmt(data['debt'])}`. Используй `!loan take сумма` или `!loan pay сумма`."
        due = as_utc(data.get("loan_due"))
        if int(data.get("debt", 0)) > 0 and due:
            if now() > due:
                text += "\n🚨 Кредит просрочен."
                if cfg["loan_penalty"] > 0:
                    text += (f" К долгу добавляется {fmt_pct(cfg['loan_penalty'])} "
                             "за каждые сутки просрочки.")
            else:
                text += f"\n📅 Погасить до: <t:{int(due.timestamp())}:f> (<t:{int(due.timestamp())}:R>)."
        await safe_send(ctx, text)

    @loan.command(name="take", description="Взять кредит из резерва ЦБ")
    @app_commands.describe(amount="Сумма кредита до начисления процентов")
    async def loan_take(self, ctx, amount: int):
        if amount < LOAN_MIN or amount > LOAN_MAX:
            return await safe_send(ctx, f"❌ Сумма должна быть от {fmt(LOAN_MIN)} до {fmt(LOAN_MAX)}.")
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        if int(user.get("debt", 0)) > 0:
            return await safe_send(ctx, "❌ Новый кредит недоступен, пока не погашен текущий долг.")
        cfg = await self.bot.get_settings(gid)
        owed = int(amount * (1 + cfg["loan_interest"]))
        due_at = now() + timedelta(seconds=LOAN_TERM)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.change_reserve(-amount, session=session):
                        raise EconomyError("В резерве ЦБ недостаточно средств.")
                    taken = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "debt": 0},
                        {"$inc": {"cash": amount, "debt": owed},
                         "$set": {"updated_at": now(), "loan_penalty_at": None, "loan_due": due_at}},
                        session=session,
                    )
                    if taken.modified_count != 1:
                        raise EconomyError("Новый кредит недоступен, пока не погашен текущий долг.")
                    await self.bot.journal("central_bank", f"user_{uid}", amount,
                                           "Выдача кредита", {"repayment_due": owed},
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        text = (f"✅ Получено **{fmt(amount)}**. К погашению: **{fmt(owed)}** "
                f"(процент {fmt_pct(cfg['loan_interest'])}).\n"
                f"📅 Срок: до <t:{int(due_at.timestamp())}:f>.")
        if cfg["loan_penalty"] > 0:
            text += f" Просрочка: +{fmt_pct(cfg['loan_penalty'])} к долгу за каждые сутки."
        await safe_send(ctx, text)

    @loan.command(name="pay", description="Погасить кредит наличными")
    @app_commands.describe(amount="Сумма погашения")
    async def loan_pay(self, ctx, amount: int):
        if amount <= 0:
            return await safe_send(ctx, "❌ Сумма должна быть положительной.")
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        debt = int(user.get("debt", 0))
        if debt <= 0:
            return await safe_send(ctx, "У тебя нет долга.")
        payment = min(amount, debt)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    # Условный апдейт: ни cash, ни debt не могут уйти в минус.
                    result = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "cash": {"$gte": payment}, "debt": {"$gte": payment}},
                        {"$inc": {"cash": -payment, "debt": -payment}, "$set": {"updated_at": now()}},
                        session=session,
                    )
                    if result.modified_count != 1:
                        raise EconomyError("Недостаточно наличных для такого платежа.")
                    await self.bot.change_reserve(payment, session=session)
                    if payment >= debt:
                        await self.bot.db_users.update_one(
                            {**ukey(gid, uid), "debt": 0},
                            {"$set": {"loan_due": None, "loan_penalty_at": None}},
                            session=session,
                        )
                    await self.bot.journal(f"user_{uid}", "central_bank", payment,
                                           "Погашение кредита", session=session, guild_id=gid)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        await safe_send(ctx, f"✅ Погашено **{fmt(payment)}**. Остаток долга: **{fmt(debt - payment)}**.")

    # --- ЦБ (глобальный) -----------------------------------------------------
    @commands.hybrid_command(name="cb", description="Состояние Центрального банка (общий для всех серверов)")
    async def cb(self, ctx):
        s = await self.bot.stats()
        await safe_send(
            ctx,
            f"🏦 **Центральный банк** (общий для всех серверов)\nРезерв: `{fmt(s['reserve'])}`\n"
            f"Фонды: `{fmt(s['funds'])}`\nГосбюджет: `{fmt(s['budget'])}`\n"
            f"Игроки (все серверы): `{fmt(s['players'])}`\nВ обращении: `{fmt(s['circulation'])}`\n"
            f"Дефицит ЦБ: `{fmt(s['debt'])}`\nНапечатано: `{fmt(s['printed'])}`"
        )

    @commands.hybrid_command(name="economy", description="Статистика экономики этого сервера")
    async def economy(self, ctx):
        s = await self.bot.stats(ctx.guild.id)
        await safe_send(
            ctx,
            f"📈 **Экономика сервера**\nИгроки `{fmt(s['players'])}` | "
            f"Долги игроков `{fmt(s['player_debt'])}`\n"
            f"За 24ч: приход `{fmt(s['incoming'])}` / расход `{fmt(s['outgoing'])}`\n"
            f"🏦 Общий ЦБ: резерв `{fmt(s['reserve'])}` | госбюджет `{fmt(s['budget'])}` | "
            f"фонды `{fmt(s['funds'])}`"
        )

    @commands.hybrid_command(name="print_money", description="Эмиссия средств в общий резерв ЦБ")
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

    @commands.hybrid_command(name="burn_money", description="Сжечь средства из общего резерва ЦБ")
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


PAYTABLES = {
    "slot": "🎰 **Выплаты слотов 3x3**\nСовпавшие символы на линии (ряд, столбец или диагональ):\n1 линия — **x2**, 2 линии — **x4**, 3 и больше — **x10**\nИначе — 0",
    "rr": "🔫 **Русская рулетка**\nШанс выигрыша 1 из 6 — выплата **x5**.",
}


async def reply_ephemeral(interaction: discord.Interaction, text: str):
    """Ответ, видимый только игроку. Безопасен, даже если на interaction уже ответили."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.warning("Не удалось отправить ephemeral-ответ")


class PlayAgainButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Играть снова", style=discord.ButtonStyle.success, emoji="🔄")

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_play_again(interaction)


class PayoutsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Выплаты", style=discord.ButtonStyle.secondary, emoji="📋")

    async def callback(self, interaction: discord.Interaction):
        await reply_ephemeral(interaction, PAYTABLES.get(self.view.game, "—"))


class CasinoView(discord.ui.View):
    """Общая база для всех игр: владелец партии, блокировка, «Играть снова»."""

    def __init__(self, cog, guild_id: int, user_id: int, bet: int, *, timeout: float):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.key = (guild_id, user_id)  # ключ активной партии блэкджека
        self.bet = bet
        self.lock = asyncio.Lock()
        self.message = None
        self.again_btn = PlayAgainButton()

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await reply_ephemeral(interaction, "Это не твоя игра.")
            return False
        return True

    async def has_funds(self, interaction: discord.Interaction) -> bool:
        user = await self.cog.bot.ensure_user(self.guild_id, self.user_id)
        if int(user.get("cash", 0)) < self.bet:
            await reply_ephemeral(interaction, "❌ Недостаточно средств.")
            return False
        return True

    async def _show(self, interaction, embed=None):
        """Обновляет текущее сообщение (а не шлёт новое)."""
        kwargs = {"view": self}
        if embed is not None:
            kwargs["embed"] = embed
        try:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(**kwargs)
            elif self.message is not None:
                await self.message.edit(**kwargs)
        except (discord.Forbidden, discord.NotFound):
            log.warning("Не удалось обновить сообщение игры (нет прав или оно удалено)")
        except discord.HTTPException:
            log.exception("Ошибка обновления сообщения игры")

    async def on_play_again(self, interaction: discord.Interaction):
        raise NotImplementedError


class InstantGameView(CasinoView):
    """Слоты и рулетка: результат известен сразу, под ним — «Играть снова» и «Выплаты»."""

    def __init__(self, cog, guild_id: int, user_id: int, bet: int, game: str):
        super().__init__(cog, guild_id, user_id, bet, timeout=PLAY_AGAIN_TIMEOUT)
        self.game = game
        self.add_item(self.again_btn)
        self.add_item(PayoutsButton())

    async def on_play_again(self, interaction: discord.Interaction):
        async with self.lock:
            if not await self.has_funds(interaction):
                return
            if self.game == "slot":
                await interaction.response.defer()
                for child in self.children:
                    child.disabled = True
                if self.message is not None:
                    await self.message.edit(view=self)
                async def progress(embed):
                    if self.message is not None:
                        await self.message.edit(embed=embed, view=self)
                try:
                    embed = await self.cog.run_game(
                        self.game, self.guild_id, self.user_id, self.bet, progress_callback=progress
                    )
                except EconomyError as e:
                    for child in self.children:
                        child.disabled = False
                    if self.message is not None:
                        await self.message.edit(content=f"❌ {e}", embed=None, view=self)
                    return
                for child in self.children:
                    child.disabled = False
                await self._show(None, embed)
                return
            try:
                embed = await self.cog.run_game(self.game, self.guild_id, self.user_id, self.bet)
            except EconomyError as e:
                return await reply_ephemeral(interaction, f"❌ {e}")
            await self._show(interaction, embed)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        await self._show(None)


class BlackjackView(CasinoView):
    def __init__(self, cog, guild_id: int, user_id: int, bet: int):
        super().__init__(cog, guild_id, user_id, bet, timeout=BJ_TIMEOUT)
        self.player = [draw_card(), draw_card()]
        self.dealer = [draw_card(), draw_card()]
        self.done = False

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

    def _lock_game_buttons(self, disabled: bool):
        for child in self.children:
            if child is not self.again_btn:
                child.disabled = disabled

    def new_round(self):
        """Сбрасывает состояние: новые карты, кнопки «Взять/Хватит» снова активны."""
        self.player = [draw_card(), draw_card()]
        self.dealer = [draw_card(), draw_card()]
        self.done = False
        self._lock_game_buttons(False)
        self.remove_item(self.again_btn)

    async def _finish(self, interaction, outcome, *, offer_again=True):
        """Завершает партию и рассчитывает выплату. Вызывать с захваченным self.lock."""
        self.done = True
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
                await self.cog.settle_payout(self.guild_id, self.user_id, payout, "blackjack")
                text += f"\nВыплата: **{fmt(payout)}**."
            except Exception:
                log.exception("Не удалось выплатить выигрыш blackjack guild=%s uid=%s payout=%s",
                              self.guild_id, self.user_id, payout)
                text += "\n⚠️ Ошибка выплаты — обратись к администратору."
        self.cog.active_bj.discard(self.key)
        self._lock_game_buttons(True)
        if offer_again:
            if self.again_btn not in self.children:
                self.add_item(self.again_btn)
            self.again_btn.disabled = False
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

    async def on_play_again(self, interaction: discord.Interaction):
        async with self.lock:
            if not self.done:
                return await interaction.response.defer()
            if not await self.has_funds(interaction):
                return
            if self.key in self.cog.active_bj:
                return await reply_ephemeral(interaction, "❌ У тебя уже идёт другая партия в блэкджек.")
            self.cog.active_bj.add(self.key)
            try:
                await self.cog.precheck(self.guild_id, self.user_id, self.bet)
                async with await self.cog.bot.mongo.start_session() as session:
                    async with session.start_transaction():
                        await self.cog.take_bet(self.guild_id, self.user_id, self.bet, "blackjack",
                                                max_payout=self.bet * 2, session=session)
            except EconomyError as e:
                self.cog.active_bj.discard(self.key)
                return await reply_ephemeral(interaction, f"❌ {e}")
            except Exception:
                self.cog.active_bj.discard(self.key)
                raise
            self.new_round()
            if hand_value(self.player) == 21:
                await self._finish(interaction, "twentyone")
            else:
                await self._show(interaction, self.render())

    async def on_timeout(self):
        async with self.lock:
            if not self.done:
                # Игрок не ответил: авто-«Хватит», без кнопки «Играть снова» (view уже остановлен).
                await self._finish(None, "stand", offer_again=False)
            else:
                self.again_btn.disabled = True
                await self._show(None)


class CasinoCog(commands.Cog):
    """Все ставки идут в резерв ЦБ, все выигрыши выплачиваются из резерва ЦБ."""

    def __init__(self, bot: EconomyBot):
        self.bot = bot
        self.active_bj = set()  # (guild_id, user_id) с незавершённой партией (эфемерное состояние)

    async def _resolve_bet(self, ctx, raw):
        """Ставка числом или 'all' (все наличные, но не больше CASINO_MAX_BET). None — ввод неверный."""
        user = await self.bot.ensure_user(ctx.guild.id, ctx.author.id)
        cash = int(user.get("cash", 0))
        bet = parse_amount(raw, min(cash, CASINO_MAX_BET))
        if bet is None:
            await safe_send(ctx, "❌ Укажи ставку числом или `all`.")
        return bet

    async def precheck(self, gid: int, uid: int, bet: int):
        if bet < CASINO_MIN_BET or bet > CASINO_MAX_BET:
            raise EconomyError(f"Ставка должна быть от {fmt(CASINO_MIN_BET)} до {fmt(CASINO_MAX_BET)}.")
        user = await self.bot.ensure_user(gid, uid)
        cash = int(user.get("cash", 0))
        if cash < bet:
            raise EconomyError(f"Недостаточно наличных: у тебя `{fmt(cash)}`, ставка `{fmt(bet)}`.")

    async def take_bet(self, gid: int, uid: int, bet: int, game: str, *, max_payout: int, session):
        """Списывает ставку с cash в резерв ЦБ. Отказывает, если резерв (вместе со ставкой)
        не покроет максимально возможную выплату."""
        debit = await self.bot.db_users.update_one(
            {**ukey(gid, uid), "cash": {"$gte": bet}},
            {"$inc": {"cash": -bet}, "$set": {"updated_at": now()}},
            session=session,
        )
        if debit.modified_count != 1:
            raise EconomyError("Недостаточно наличных.")
        await self.bot.change_reserve(bet, session=session)
        if await self.bot.reserve(session=session) < max_payout:
            raise EconomyError("Резерв ЦБ слишком мал, чтобы покрыть возможный выигрыш. "
                               "Уменьши ставку или попроси админа сделать эмиссию.")
        key = game.replace("_refund", "")
        await self.bot.casino_stats.update_one(
            ukey(gid, uid),
            {"$inc": {"games": 1, "wagered": bet,
                      f"by_game.{key}.games": 1, f"by_game.{key}.wagered": bet},
             "$set": {"updated_at": now()}},
            upsert=True, session=session,
        )
        await self.bot.journal(f"user_{uid}", "central_bank", bet, f"Казино: ставка ({game})",
                               {"game": game}, session=session, guild_id=gid)

    async def pay_out(self, gid: int, uid: int, payout: int, game: str, *, session, force=False):
        """Выплата из резерва ЦБ. force=True — для уже принятых ставок (резерв может уйти в дефицит)."""
        if payout <= 0:
            return
        if not await self.bot.change_reserve(-payout, session=session, allow_negative=force):
            raise EconomyError("Резерв ЦБ не может покрыть выигрыш.")
        await self.bot.change_cash(gid, uid, payout, session=session)
        key = game.replace("_refund", "")
        await self.bot.casino_stats.update_one(
            ukey(gid, uid),
            {"$inc": {"paid": payout, f"by_game.{key}.paid": payout}, "$set": {"updated_at": now()}},
            upsert=True, session=session,
        )
        await self.bot.journal("central_bank", f"user_{uid}", payout, f"Казино: выплата ({game})",
                               {"game": game}, session=session, guild_id=gid)

    async def settle_payout(self, gid: int, uid: int, payout: int, game: str):
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.pay_out(gid, uid, payout, game, session=session, force=True)

    # --- Слоты и рулетка (общий движок + «Играть снова») ---------------------
    def _result_embed(self, title, body, bet, payout, cash, color):
        embed = discord.Embed(
            title=title,
            description=f"{body}\n\n**Выплата:** {COIN_EMOJI} {fmt(payout)}",
            color=color,
        )
        embed.set_footer(text=f"Ставка: {fmt(bet)} • Наличные: {fmt(cash)}")
        return embed

    @staticmethod
    def _slot_grid_text(grid):
        return "\n".join(" │ ".join(row) for row in grid)

    @staticmethod
    def _slot_winning_lines(grid):
        # 3 ряда, 3 столбца и 2 диагонали — всего 8 линий.
        lines = [
            grid[0], grid[1], grid[2],
            [grid[0][0], grid[1][0], grid[2][0]],
            [grid[0][1], grid[1][1], grid[2][1]],
            [grid[0][2], grid[1][2], grid[2][2]],
            [grid[0][0], grid[1][1], grid[2][2]],
            [grid[0][2], grid[1][1], grid[2][0]],
        ]
        return [line for line in lines if line[0] == line[1] == line[2]]

    async def run_slot(self, gid: int, uid: int, bet: int, progress_callback=None):
        """3x3-слоты. Совпавшие тройки на линиях: x2 / x4 / x10 за 1 / 2 / 3+ линий.
        Ставка и выплата проводятся одной MongoDB-транзакцией."""
        await self.precheck(gid, uid, bet)
        final_grid = [[rng.choice(SLOT_SYMBOLS) for _ in range(3)] for _ in range(3)]

        # Короткая анимация: несколько обновлений одного сообщения.
        if progress_callback is not None:
            for frame in range(3):
                animated_grid = [
                    [rng.choice(SLOT_SYMBOLS) for _ in range(3)]
                    for _ in range(3)
                ]
                anim_embed = discord.Embed(
                    title="🎰 Слоты крутятся…",
                    description=f"```\n{self._slot_grid_text(animated_grid)}\n```",
                    color=discord.Color.blurple(),
                )
                anim_embed.set_footer(text=f"Ставка: {fmt(bet)} • Кадр {frame + 1}/3")
                try:
                    await progress_callback(anim_embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.warning("Не удалось показать кадр анимации слотов")
                    progress_callback = None
                    break
                await asyncio.sleep(1)

        winning_lines = self._slot_winning_lines(final_grid)
        line_count = len(winning_lines)
        multiplier = 0 if line_count == 0 else (2 if line_count == 1 else 4 if line_count == 2 else 10)
        payout = bet * multiplier

        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.take_bet(gid, uid, bet, "slot", max_payout=bet * 10, session=session)
                await self.pay_out(gid, uid, payout, "slot", session=session)

        cash = int((await self.bot.get_balance(gid, uid)).get("cash", 0))
        title = (
            f"🎉 Джекпот! x{multiplier}" if line_count >= 3 else
            f"✨ Выигрыш x{multiplier}" if line_count else
            "🎰 Не повезло в этот раз"
        )
        color = discord.Color.gold() if line_count >= 3 else (
            discord.Color.green() if line_count else discord.Color.red()
        )
        body = f"```\n{self._slot_grid_text(final_grid)}\n```"
        if line_count:
            body += f"\nСовпавших линий: **{line_count}** из 8."
        else:
            body += "\nСовпавших линий нет."
        embed = self._result_embed(title, body, bet, payout, cash, color)
        embed.set_footer(text=f"Ставка: {fmt(bet)} • Наличные: {fmt(cash)}")
        return embed

    async def run_rr(self, gid: int, uid: int, bet: int):
        await self.precheck(gid, uid, bet)
        won = rng.randint(1, 6) == 1
        payout = bet * 5 if won else 0
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.take_bet(gid, uid, bet, "russian_roulette", max_payout=bet * 5, session=session)
                await self.pay_out(gid, uid, payout, "russian_roulette", session=session)
        cash = int((await self.bot.get_balance(gid, uid)).get("cash", 0))
        if won:
            return self._result_embed("🔫 Тебе повезло! x5", "*щёлк…* Пустая камера.",
                                      bet, payout, cash, discord.Color.green())
        return self._result_embed("💥 БАХ!", "Ты потерял ставку.", bet, payout, cash, discord.Color.red())

    async def run_game(self, game: str, gid: int, uid: int, bet: int, progress_callback=None):
        """Одна партия мгновенной игры: списывает ставку, платит выигрыш, возвращает эмбед."""
        if game == "slot":
            return await self.run_slot(gid, uid, bet, progress_callback=progress_callback)
        if game == "rr":
            return await self.run_rr(gid, uid, bet)
        raise ValueError(game)

    async def _start_instant(self, ctx, game: str, bet: int):
        gid, uid = ctx.guild.id, ctx.author.id
        view = InstantGameView(self, gid, uid, bet, game)
        if game == "slot":
            for child in view.children:
                child.disabled = True
            placeholder = discord.Embed(
                title="🎰 Слоты запускаются…",
                description="```\n❔ │ ❔ │ ❔\n❔ │ ❔ │ ❔\n❔ │ ❔ │ ❔\n```",
                color=discord.Color.blurple(),
            )
            view.message = await safe_send(ctx, embed=placeholder, view=view)
            if view.message is None:
                return
            async def progress(embed):
                await view.message.edit(embed=embed, view=view)
            try:
                embed = await self.run_game(game, gid, uid, bet, progress_callback=progress)
            except EconomyError as e:
                await view.message.edit(content=f"❌ {e}", embed=None, view=None)
                return
            for child in view.children:
                child.disabled = False
            await view.message.edit(embed=embed, view=view)
            return
        try:
            embed = await self.run_game(game, gid, uid, bet)
        except EconomyError as e:
            return await safe_send(ctx, f"❌ {e}")
        view.message = await safe_send(ctx, embed=embed, view=view)

    GAME_LABELS = {"slot": "🎰 Слоты", "russian_roulette": "🔫 Рулетка", "blackjack": "🃏 Блэкджек"}

    @commands.hybrid_command(name="casinostats", aliases=["cstats"], description="Статистика казино игрока")
    @app_commands.describe(member="Чью статистику показать")
    async def casinostats(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        doc = await self.bot.casino_stats.find_one(ukey(ctx.guild.id, member.id))
        if not doc or not int(doc.get("games", 0)):
            return await safe_send(ctx, f"🎲 {member.display_name} ещё не играл в казино.")
        wagered, paid, games = int(doc.get("wagered", 0)), int(doc.get("paid", 0)), int(doc.get("games", 0))
        net = paid - wagered
        rtp = paid / wagered * 100 if wagered else 0
        embed = discord.Embed(
            title=f"🎲 Казино — {member.display_name}",
            color=discord.Color.green() if net >= 0 else discord.Color.red(),
        )
        embed.add_field(name="Игр", value=f"`{fmt(games)}`", inline=True)
        embed.add_field(name="Поставлено", value=f"`{fmt(wagered)}`", inline=True)
        embed.add_field(name="Выплачено", value=f"`{fmt(paid)}`", inline=True)
        embed.add_field(name="Итог", value=f"`{'+' if net >= 0 else '-'}{fmt(abs(net))}` ({rtp:.0f}% возврата)", inline=False)
        lines = []
        for key, row in (doc.get("by_game") or {}).items():
            g_net = int(row.get("paid", 0)) - int(row.get("wagered", 0))
            lines.append(f"{self.GAME_LABELS.get(key, key)}: {fmt(row.get('games', 0))} игр, "
                         f"{'+' if g_net >= 0 else '-'}{fmt(abs(g_net))}")
        if lines:
            embed.add_field(name="По играм", value="\n".join(lines), inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await safe_send(ctx, embed=embed)

    @commands.hybrid_command(name="slot", description="Слоты 3x3: выигрышные линии x2, x4 или x10")
    @app_commands.describe(bet="Ставка наличными или all")
    async def slot(self, ctx, bet: str):
        bet = await self._resolve_bet(ctx, bet)
        if bet is None:
            return
        await self._start_instant(ctx, "slot", bet)

    @commands.hybrid_command(name="rr", description="Русская рулетка: шанс 1 к 6, выигрыш x5")
    @app_commands.describe(bet="Ставка наличными или all")
    async def rr(self, ctx, bet: str):
        bet = await self._resolve_bet(ctx, bet)
        if bet is None:
            return
        await self._start_instant(ctx, "rr", bet)

    # --- Блэкджек ------------------------------------------------------------
    @commands.hybrid_command(name="bj", description="Блэкджек: набери 21, дилер берёт до 17")
    @app_commands.describe(bet="Ставка наличными или all")
    async def bj(self, ctx, bet: str):
        gid, uid = ctx.guild.id, ctx.author.id
        key = (gid, uid)
        if key in self.active_bj:
            return await safe_send(ctx, "❌ У тебя уже идёт партия в блэкджек.")
        bet = await self._resolve_bet(ctx, bet)
        if bet is None:
            return
        self.active_bj.add(key)
        try:
            await self.precheck(gid, uid, bet)
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    await self.take_bet(gid, uid, bet, "blackjack", max_payout=bet * 2, session=session)
        except EconomyError as e:
            self.active_bj.discard(key)
            return await safe_send(ctx, f"❌ {e}")
        except Exception:
            self.active_bj.discard(key)
            raise

        view = BlackjackView(self, gid, uid, bet)
        message = await safe_send(ctx, embed=view.render(), view=view)
        if message is None:
            # Не смогли показать игру (нет прав/канал удалён) — возвращаем ставку.
            try:
                await self.settle_payout(gid, uid, bet, "blackjack_refund")
            except Exception:
                log.exception("Не удалось вернуть ставку blackjack guild=%s uid=%s bet=%s", gid, uid, bet)
            self.active_bj.discard(key)
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
            note = "\n⚠️ Но на стороне бота не задан `AI_API_KEY` или `GROQ_API_KEY`, поэтому ИИ фактически недоступен."
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
        stats = await self.bot.stats(ctx.guild.id)
        try:
            text = await self.bot.ai.market_report(stats)
        except AIError as e:
            return await safe_send(ctx, f"⚠️ Не удалось получить ИИ-отчёт: {e}")
        if len(text) > 1900:
            text = text[:1900].rstrip() + "…"
        await safe_send(ctx, f"🤖 **ИИ-отчёт по экономике**\n{text}")


# =============================================================================
# Панель настроек /settings
# =============================================================================
def preset_label(preset):
    return PRESETS[preset]["label"] if preset in PRESETS else "⚙️ Кастомная"


def settings_embed(guild, cfg):
    colors = {"easy": discord.Color.green(), "standard": discord.Color.gold(),
              "hardcore": discord.Color.red()}
    embed = discord.Embed(
        title=f"⚙️ Настройки экономики — {guild.name}",
        description=(f"Текущий пресет: **{preset_label(cfg['preset'])}**\n"
                     "Выбери пресет в меню ниже. «⚙️ Кастомная» откроет ручную настройку "
                     "шансов, налогов, кулдаунов и зарплаты."),
        color=colors.get(cfg["preset"], discord.Color.blurple()),
    )
    embed.add_field(
        name="⏱️ Кулдауны",
        value="\n".join(f"`!{k}` — {fmt_cooldown(cfg[f'cooldown_{k}'])}" for k in COOLDOWN_KEYS),
        inline=True,
    )
    embed.add_field(
        name="🎲 Шансы",
        value=(f"`!crime`, `!slut`: {fmt_pct(cfg['crime_chance'])}\n"
               f"`!rob`: {fmt_pct(cfg['rob_chance'])}"),
        inline=True,
    )
    embed.add_field(
        name="💰 Заработок и налоги",
        value=(f"`!work`: {fmt(cfg['work_min'])}–{fmt(cfg['work_max'])}\n"
               f"`!daily`: {fmt(cfg['daily_min'])}–{fmt(cfg['daily_max'])}\n"
               f"НДФЛ: {fmt_pct(cfg['tax_rate'])} • налог на бизнес: {fmt_pct(cfg['business_tax'])}"),
        inline=False,
    )
    embed.add_field(
        name="🏦 Кредит",
        value=(f"Процент: {fmt_pct(cfg['loan_interest'])} • "
               f"штраф просрочки: {fmt_pct(cfg['loan_penalty'])} в сутки"),
        inline=False,
    )
    embed.set_footer(text="Изменения применяются сразу. Резерв ЦБ и госбюджет — общие для всех серверов.")
    return embed


class PresetSelect(discord.ui.Select):
    def __init__(self, current: str):
        options = []
        for key, preset in PRESETS.items():
            emoji, name = preset["label"].split(" ", 1)
            options.append(discord.SelectOption(
                label=name, value=key, emoji=emoji,
                description=preset["description"][:100], default=(key == current)))
        options.append(discord.SelectOption(
            label="Кастомная", value="custom", emoji="⚙️",
            description="Ввести шансы, налоги и кулдауны вручную", default=(current == "custom")))
        super().__init__(placeholder="Выбери пресет экономики", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await self.view.apply_preset(interaction, self.values[0])


class SectionSelect(discord.ui.Select):
    """Второе меню (только в режиме «Кастомная»): какой раздел настроек открыть в форме."""

    def __init__(self):
        options = []
        for key, section in SETTING_SECTIONS.items():
            emoji, name = section["label"].split(" ", 1)
            options.append(discord.SelectOption(label=name, value=key, emoji=emoji))
        super().__init__(placeholder="Что настроить вручную?", min_values=1, max_values=1,
                         options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        cfg = await self.view.bot.get_settings(self.view.guild.id)
        await interaction.response.send_modal(SettingsModal(self.view, self.values[0], cfg))


class SettingsView(discord.ui.View):
    def __init__(self, bot, author_id: int, guild: discord.Guild, cfg: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = author_id
        self.guild = guild
        self.message = None
        self.add_item(PresetSelect(cfg["preset"]))
        if cfg["preset"] == "custom":
            self.add_item(SectionSelect())

    async def interaction_check(self, interaction: discord.Interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id != self.author_id or not (perms and perms.administrator):
            await reply_ephemeral(interaction, "Эта панель доступна только администратору, открывшему её.")
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        """Перерисовывает панель по актуальным настройкам (пересоздаём View под новый режим)."""
        cfg = await self.bot.get_settings(self.guild.id)
        new_view = SettingsView(self.bot, self.author_id, self.guild, cfg)
        embed = settings_embed(self.guild, cfg)
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
            new_view.message = interaction.message or self.message
        except (discord.HTTPException, TypeError, ValueError):
            new_view.message = self.message
            if new_view.message is not None:
                try:
                    await new_view.message.edit(embed=embed, view=new_view)
                except (discord.HTTPException, TypeError, ValueError):
                    log.warning("Не удалось обновить панель настроек")
            await reply_ephemeral(interaction, "✅ Сохранено.")
        self.stop()

    async def apply_preset(self, interaction: discord.Interaction, choice: str):
        gid = self.guild.id
        if choice == "custom":
            # Значения остаются прежними — админ дальше правит их вручную.
            update = {"$set": {"eco.preset": "custom", "updated_at": now()}}
        else:
            update = {
                "$set": {"eco": {"preset": choice, **PRESETS[choice]["values"]}, "updated_at": now()},
                "$unset": {f"cooldown_{key}": "" for key in COOLDOWN_KEYS},  # старый формат
            }
        await self.bot.settings.update_one({"_id": gid}, update, upsert=True)
        self.bot.invalidate_settings(gid)
        log.info("Guild %s: пресет экономики -> %s", gid, choice)
        await self.refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, TypeError, ValueError):
                pass


class SettingsModal(discord.ui.Modal):
    """Форма ручной настройки одного раздела (в Discord — не больше 5 полей)."""

    def __init__(self, panel: SettingsView, section: str, cfg: dict):
        spec = SETTING_SECTIONS[section]
        super().__init__(title=spec["title"][:45], timeout=300)
        self.panel = panel
        self.inputs = {}
        for key in spec["keys"]:
            _kind, _lo, _hi, label, hint = SETTING_SPECS[key]
            item = discord.ui.TextInput(
                label=label[:45], default=setting_input_default(key, cfg[key]),
                placeholder=hint[:100], required=True, max_length=20)
            self.add_item(item)
            self.inputs[key] = item

    async def on_submit(self, interaction: discord.Interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and perms.administrator):
            return await reply_ephemeral(interaction, "❌ Нужны права администратора.")
        values, errors = {}, []
        for key, item in self.inputs.items():
            try:
                values[key] = parse_setting(key, item.value)
            except ValueError as e:
                errors.append(f"• **{SETTING_SPECS[key][3]}**: {e}")
        gid = self.panel.guild.id
        merged = {**await self.panel.bot.get_settings(gid), **values}
        if merged["work_min"] > merged["work_max"]:
            errors.append("• **Зарплата !work**: минимум больше максимума")
        if merged["daily_min"] > merged["daily_max"]:
            errors.append("• **Бонус !daily**: минимум больше максимума")
        if errors:
            return await reply_ephemeral(interaction, "❌ Не сохранено:\n" + "\n".join(errors))
        update = {"$set": {**{f"eco.{key}": value for key, value in values.items()},
                           "eco.preset": "custom", "updated_at": now()}}
        await self.panel.bot.settings.update_one({"_id": gid}, update, upsert=True)
        self.panel.bot.invalidate_settings(gid)
        log.info("Guild %s: ручные настройки экономики %s", gid, values)
        await self.panel.refresh(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Ошибка формы настроек", exc_info=error)
        await reply_ephemeral(interaction, "⚠️ Внутренняя ошибка. Попробуй ещё раз.")


class SettingsCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot

    @commands.hybrid_command(name="settings", description="Панель настроек экономики сервера (админ)")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def settings_panel(self, ctx):
        cfg = await self.bot.get_settings(ctx.guild.id)
        view = SettingsView(self.bot, ctx.author.id, ctx.guild, cfg)
        view.message = await safe_send(ctx, embed=settings_embed(ctx.guild, cfg), view=view, ephemeral=True)


def main():
    check_env()
    bot = EconomyBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
