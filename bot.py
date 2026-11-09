import os
import io
import json
import math
import asyncio
import logging
import urllib.parse
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
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
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

BOT_VERSION = "7.6.0"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "economy_bot")
PORT = int(os.getenv("PORT", "10000"))

# ====================== ЭКОНОМИКА ======================
LOAN_MIN, LOAN_MAX = 100, 1_000_000
LOAN_TERM = 3 * 24 * 3600
REPORT_HOUR_UTC = 18
COOLDOWN_MAX = 7 * 24 * 3600

# ====================== КРИМИНАЛ ======================
CRIME_REWARD = (300, 900)
SLUT_REWARD = (200, 700)
CRIME_FINE_RATE = 0.20
CRIME_FINE_MAX = 500
ROB_LOOT_RATE = (0.10, 0.30)
ROB_MAX_LOOT = 5_000
ROB_MIN_TARGET_CASH = 500
ROB_FINE_RATE = 0.25
ROB_FINE_MAX = 1_000
ROB_VICTIM_PROTECTION = 3600

# ====================== БИЗНЕС ======================
BUSINESS_COST = 50_000
BUSINESS_HOURLY_INCOME = 1_000
BUSINESS_INCOME_PER_LEVEL = 500
BUSINESS_UPGRADE_BASE_COST = 25_000
BUSINESS_MAX_LEVEL = 10

# ====================== КАЗИНО ======================
CASINO_MIN_BET = 10
CASINO_MAX_BET = 100_000
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "💎", "7️⃣"]
# Множители слотов по числу совпавших линий (3+ линий — одна ступень).
# Сетка 3x3, 5 символов, 8 линий. Точное распределение (5^9 исходов):
#   0 линий 71.86% | 1 линия 24.78% | 2 линии 2.94% | 3+ линий 0.40%
# RTP = 0.24781*3 + 0.02943*4 + 0.00415*10 ≈ 90.3%  (было ×2/×4/×10 ≈ 65.5%)
SLOT_PAYOUTS = {1: 3, 2: 4, 3: 10}
SLOT_MAX_MULT = max(SLOT_PAYOUTS.values())

def slot_multiplier(line_count: int) -> int:
    if line_count <= 0:
        return 0
    return SLOT_PAYOUTS[min(line_count, 3)]
PLAY_AGAIN_TIMEOUT = 120
COIN_EMOJI = "🪙"
BJ_TIMEOUT = 90

# ====================== БИРЖА ======================
STOCK_CREATE_COST = 100_000        # стоимость IPO (уходит в резерв ЦБ)
STOCK_START_PRICE = 100.0
STOCK_START_SUPPLY = 1000          # акций выпускается при IPO
STOCK_TRADE_STEP = 0.01            # ±1% цены за сделку
STOCK_TICK_MAX = 0.03              # случайное движение раз в тик: ±3%
STOCK_TICK_MINUTES = 30
STOCK_HISTORY_MAX = 5000           # точек истории на акцию
STOCK_TRADE_COOLDOWN = 10          # сек между сделками одного игрока (анти-памп)
STOCK_TICKER_RE = re.compile(r"^[A-ZА-ЯЁ0-9]{2,6}$")
STOCK_AUTO_TICKER_LEN = 4          # авто-тикер: первые 4 буквы названия
STOCK_LIST_BUTTONS = 5             # кнопок под !stock list (самые дорогие акции)

# ====================== КАСТОМНЫЕ ТЕКСТЫ (/settings) ======================
CUSTOM_TEXT_LABELS = {
    "crime_win": "Успех !crime",
    "crime_lose": "Провал !crime",
    "slut_win": "Успех !slut",
    "slut_lose": "Провал !slut",
}
CUSTOM_TEXT_KEYS = tuple(CUSTOM_TEXT_LABELS)
CUSTOM_TEXT_MAX = 1000

# ====================== РУССКАЯ РУЛЕТКА ======================
RR_BULLET_SIDES = 6                # шанс пули = 1/6 на каждый выстрел
RR_EDGE = 0.98                     # «комиссия» казино: множитель = 0.98 * 1.2^n  (1.18, 1.41, 1.69 …)
RR_STEP_GROWTH = 6 / 5             # честный рост при шансе выжить 5/6
RR_MAX_STEPS = 10                  # после 10 пережитых выстрелов выплата выдаётся автоматически
RR_LOBBY_SECONDS = 15              # ожидание игроков в лобби
RR_MAX_PLAYERS = 6
RR_TURN_TIMEOUT = 45               # секунд на ход (иначе авто-действие)
RR_MODE_TIMEOUT = 60               # секунд на выбор режима

# ====================== ПРЕСЕТЫ ======================
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
            "business_tax": 0.0, "daily_min": 300, "daily_max": 500,
            "inflation_multiplier": 0.0,
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
            "business_tax": 0.05, "daily_min": 300, "daily_max": 500,
            "inflation_multiplier": 0.5,
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
            "business_tax": 0.10, "daily_min": 300, "daily_max": 500,
            "inflation_multiplier": 1.0,
        },
    },
}
DEFAULT_PRESET = "standard"
DEFAULT_SETTINGS = dict(PRESETS[DEFAULT_PRESET]["values"])
COOLDOWN_KEYS = ("work", "crime", "slut", "rob", "daily")

# Модули по умолчанию — все включены
DEFAULT_MODULES = {"business": True, "casino": True, "crime": True,
                   "stocks": True, "daily": True}

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

# ====================== ИИ ======================
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
    pass

class CooldownError(EconomyError):
    pass

class TreasuryEmptyError(EconomyError):
    """В Резерве ЦБ не хватает средств на выплату."""
    pass

TREASURY_EMPTY_TEXT = ("В казне пусто, подождите пока налоги пополнят бюджет. "
                       "Админ может пополнить резерв командой `!print-money`.")


# ====================== УТИЛИТЫ ======================
def now():
    return datetime.now(timezone.utc)

def fmt(value):
    return f"{int(value):,}".replace(",", " ")

def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours: parts.append(f"{hours} ч")
    if minutes or hours: parts.append(f"{minutes} мин")
    parts.append(f"{secs} с")
    return " ".join(parts)

def parse_amount(raw, available):
    raw = str(raw or "").strip().lower()
    if raw in ("all", "max", "все", "всё"):
        return int(available)
    try:
        value = int(raw.replace(" ", "").replace("_", ""))
    except ValueError:
        return None
    return value if value > 0 else None

async def safe_create_index(collection, keys, *, drop_on_conflict=False, **options):
    try:
        return await collection.create_index(keys, **options)
    except OperationFailure as e:
        if e.code not in (85, 86):
            raise
        name = getattr(collection, "name", "?")
        if not drop_on_conflict:
            log.warning("Индекс %s.%s уже существует — оставляю", name, keys)
            return None
        log.warning("Индекс %s.%s конфликтует — пересоздаю", name, keys)
        info = await collection.index_information()
        want = [tuple(k) if isinstance(k, (list, tuple)) else (k, 1)
                for k in (keys if isinstance(keys, list) else [keys])]
        for index_name, spec in info.items():
            if index_name != "_id" and [tuple(x) for x in spec.get("key", [])] == want:
                await collection.drop_index(index_name)
                return await collection.create_index(keys, **options)
    return None

def ukey(guild_id, user_id):
    return {"guild_id": int(guild_id), "user_id": int(user_id)}

def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def fmt_pct(value):
    return f"{float(value) * 100:g}%"

def fmt_cooldown(seconds):
    seconds = int(seconds)
    if seconds <= 0: return "нет"
    parts = []
    for size, unit in ((86400, "д"), (3600, "ч"), (60, "мин"), (1, "с")):
        qty, seconds = divmod(seconds, size)
        if qty: parts.append(f"{qty}{unit}")
    return " ".join(parts)

def duration_input(seconds):
    seconds = int(seconds)
    if seconds == 0: return "0"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0: return f"{seconds // size}{unit}"
    return f"{seconds}s"

_DURATION_UNITS = {
    "": 60, "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "hr": 3600, "ч": 3600, "час": 3600,
    "d": 86400, "day": 86400, "д": 86400, "дн": 86400,
}

def parse_setting(key, raw):
    kind, lo, hi, _label, _hint = SETTING_SPECS[key]
    text = str(raw or "").strip().lower().replace(",", ".").replace(" ", "")
    if not text: raise ValueError("пустое значение")
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
    if kind == "rate": return fmt_pct(value)
    if kind == "duration": return fmt_cooldown(value)
    return fmt(value)

def setting_input_default(key, value):
    kind = SETTING_SPECS[key][0]
    if kind == "rate": return fmt_pct(value)
    if kind == "duration": return duration_input(value)
    return str(int(value))

def mongo_uri():
    user = urllib.parse.quote_plus(MONGO_USER or "")
    password = urllib.parse.quote_plus(MONGO_PASSWORD or "")
    return f"mongodb+srv://{user}:{password}@eco.lphalvu.mongodb.net/?retryWrites=true&w=majority&appName=CentralBank"

def check_env():
    missing = [k for k, v in {"DISCORD_TOKEN": DISCORD_TOKEN, "MONGO_USER": MONGO_USER,
                              "MONGO_PASSWORD": MONGO_PASSWORD}.items() if not v]
    if missing: raise RuntimeError("Не заданы: " + ", ".join(missing))

def as_utc(value):
    if not isinstance(value, datetime): return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def business_income(level):
    return BUSINESS_HOURLY_INCOME + BUSINESS_INCOME_PER_LEVEL * (max(1, int(level)) - 1)

def business_tax(level, rate):
    return int(business_income(level) * rate)

def upgrade_cost(level):
    return BUSINESS_UPGRADE_BASE_COST * max(1, int(level))

async def safe_send(target, *args, **kwargs):
    try:
        return await target.send(*args, **kwargs)
    except discord.Forbidden:
        log.warning("Нет прав на отправку в %r", target)
    except discord.NotFound:
        log.warning("Канал/сообщение не найдены: %r", target)
    except discord.HTTPException:
        log.exception("Discord отклонил сообщение (%r)", target)
    return None


# ====================== ХЕЛПЕР ЭМБЕДОВ ======================
EMBED_KINDS = {
    "success": (discord.Color.green(),  "✅"),
    "danger":  (discord.Color.red(),    "❌"),
    "warning": (discord.Color.orange(), "⚠️"),
    "info":    (discord.Color.blurple(), "ℹ️"),
}

def make_embed(kind, title, description=None, fields=None, footer=None,
               thumbnail=None, author=None):
    color, icon = EMBED_KINDS.get(kind, EMBED_KINDS["info"])
    embed = discord.Embed(
        title=f"{icon} {title}",
        description=description,
        color=color,
        timestamp=now(),
    )
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=str(value), inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if author:
        embed.set_author(name=author)
    return embed

async def send_embed(target, kind, title, description=None, fields=None,
                     footer=None, thumbnail=None, author=None, ephemeral=False):
    embed = make_embed(kind, title, description, fields, footer, thumbnail, author)
    if isinstance(target, discord.Interaction):
        try:
            if target.response.is_done():
                return await target.followup.send(embed=embed, ephemeral=ephemeral)
            return await target.response.send_message(embed=embed, ephemeral=ephemeral)
        except Exception:
            return None
    return await safe_send(target, embed=embed)


# ====================== ИИ ======================
class AIError(Exception): pass

class AIHandler:
    REPORT_PROMPT = ("Ты — финансовый аналитик. Проанализируй эти данные экономики "
                     "и напиши краткий отчет (3-4 предложения) о состоянии рынка.")
    # Запасные модели Groq, если выбранная снята с обслуживания (Groq отвечает 404/400 model_not_found)
    FALLBACK_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"]
    MODEL_ERROR_CODES = {"model_not_found", "model_decommissioned", "model_not_active",
                         "model_terms_required"}

    def __init__(self):
        self.api_key = os.getenv("AI_API_KEY") or os.getenv("GROQ_API_KEY")
        self.url = self._normalize_url(os.getenv("AI_URL"))
        model = (os.getenv("AI_MODEL") or os.getenv("GROQ_MODEL") or DEFAULT_AI_MODEL).strip()
        self.model = LEGACY_MODEL_ALIASES.get(model, model)
        self._session = None

    @staticmethod
    def _normalize_url(url) -> str:
        """Чиним типичные ошибки в AI_URL: пустая строка, база без /chat/completions."""
        url = (url or "").strip().rstrip("/")
        if not url:
            return DEFAULT_AI_URL
        if url.endswith("/chat/completions"):
            return url
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rstrip("/")
        if parsed.netloc.endswith("groq.com"):
            if path in ("", "/openai", "/openai/v1", "/v1"):
                return f"{parsed.scheme}://{parsed.netloc}/openai/v1/chat/completions"
        elif path in ("", "/v1"):
            return f"{url.rstrip('/')}{'' if path else '/v1'}/chat/completions"
        return url

    @property
    def enabled(self): return bool(self.api_key)

    @property
    def is_groq(self) -> bool:
        return "groq.com" in urllib.parse.urlparse(self.url).netloc

    async def _http(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    @staticmethod
    def _error_info(body: str):
        """(code, message) из тела ошибки в формате OpenAI/Groq."""
        try:
            err = json.loads(body).get("error")
        except (ValueError, AttributeError):
            return "", ""
        if isinstance(err, dict):
            return str(err.get("code") or err.get("type") or ""), str(err.get("message") or "")
        return "", str(err or "")

    def _model_candidates(self):
        models = [self.model]
        if self.is_groq:
            models += [m for m in self.FALLBACK_MODELS if m not in models]
        return models

    async def complete(self, system, user, *, max_tokens=400):
        if not self.enabled: raise AIError("Не задан AI_API_KEY / GROQ_API_KEY.")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        model_failed = False
        for model in self._model_candidates():
            payload = {"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}],
                "temperature": 0.4, "max_tokens": max_tokens}
            try:
                session = await self._http()
                async with session.post(self.url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.exception("AI недоступен")
                raise AIError("Не удалось связаться с ИИ-сервисом.")
            if status == 200:
                if model != self.model:
                    log.warning("AI: модель %s недоступна, переключаюсь на %s", self.model, model)
                    self.model = model
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    raise AIError("ИИ-сервис вернул некорректный ответ.")
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, AttributeError):
                    raise AIError("ИИ-сервис вернул пустой ответ.")
            code, message = self._error_info(body)
            log.error("AI HTTP %s (url=%s model=%s): %s", status, self.url, model, body[:500])
            blob = f"{code} {message}".lower()
            is_model_error = code in self.MODEL_ERROR_CODES or (
                status in (400, 404) and "model" in blob)
            if is_model_error:
                model_failed = True
                continue                      # пробуем следующую модель
            if status == 404:
                raise AIError("ИИ-эндпоинт не найден (HTTP 404). Проверь `AI_URL`.")
            if status in (401, 403):
                raise AIError("ИИ-сервис отклонил ключ API. Проверь `AI_API_KEY`.")
            if status == 429:
                raise AIError("Лимит запросов к ИИ исчерпан. Попробуй позже.")
            raise AIError(f"ИИ-сервис вернул ошибку HTTP {status}.")
        if model_failed:
            raise AIError("Модель недоступна. Администратору: проверь `AI_MODEL` / `GROQ_MODEL`.")
        raise AIError("ИИ-сервис недоступен.")

    async def market_report(self, stats):
        data = json.dumps(stats, ensure_ascii=False)
        return await self.complete(self.REPORT_PROMPT, f"Данные экономики (JSON):\n{data}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ====================== БОТ ======================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

async def guild_only_check(ctx):
    if ctx.guild is None: raise commands.NoPrivateMessage()
    return True


class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents, help_command=None,
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
        self.casino_stats = None
        self.stocks = None
        self.stock_holdings = None
        self.active_games = None
        self.ai = AIHandler()
        self._settings_cache = {}
        self._infl_cache = {}
        self.add_check(guild_only_check)
        self.chart_lock = asyncio.Lock()
        self._web_runner = None
        self.chart_files = {
            "all": "/tmp/cb_economy_all.png",
            "24h": "/tmp/cb_economy_24h.png",
        }

    # ---------- ИЗОЛЯЦИЯ: ключи документов ЦБ и бюджета ----------
    @staticmethod
    def cb_id(guild_id) -> str:
        return f"central_bank_{int(guild_id)}"

    @staticmethod
    def budget_id(guild_id) -> str:
        return f"state_budget_{int(guild_id)}"

    async def setup_hook(self):
        app = web.Application()
        async def health(request):
            return web.json_response({"status": "ok", "version": BOT_VERSION})
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, "0.0.0.0", PORT)
        await site.start()
        log.info("HTTP health on port %s", PORT)

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
        self.stocks = self.db.stocks
        self.stock_holdings = self.db.stock_holdings
        self.active_games = self.db.active_games

        await self.migrate_legacy()
        await self.migrate_stock_isolation()
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
        await safe_create_index(self.stocks, "guild_id")
        await safe_create_index(self.stocks, [("guild_id", 1), ("ticker", 1)])
        await safe_create_index(self.stock_holdings, [("guild_id", 1), ("user_id", 1), ("ticker", 1)],
                                unique=True, drop_on_conflict=True)
        await safe_create_index(self.active_games, [("guild_id", 1), ("user_id", 1)])
        await safe_create_index(self.economy, "_id")
        await safe_create_index(self.funds, "_id")

        # Создаём ЦБ и бюджет для всех серверов, на которых уже есть бот
        for guild in self.guilds:
            await self.ensure_guild_funds(guild.id)

        await self.add_cog(EconomyCog(self))
        casino = CasinoCog(self)
        await self.add_cog(casino)
        # Рестарт во время игры: возвращаем ставки всех незавершённых партий
        await casino.recover_active_games()
        await self.add_cog(StockCog(self))
        await self.add_cog(SettingsCog(self))

        if not self.ai.enabled:
            log.info("GROQ_API_KEY не задан — ИИ отключён.")
        else:
            log.info("ИИ: url=%s model=%s", self.ai.url, self.ai.model)
        try:
            synced = await self.tree.sync()
            log.info("Slash sync: %s", len(synced))
        except Exception:
            log.exception("Slash sync failed")

        self.business_loop.start()
        self.loan_loop.start()
        self.snapshot_loop.start()
        self.daily_report_loop.start()
        self.stock_loop.start()

    async def close(self):
        for loop in (self.business_loop, self.loan_loop, self.snapshot_loop,
                     self.daily_report_loop, self.stock_loop):
            if loop.is_running(): loop.cancel()
        try: await self.ai.close()
        except Exception: log.exception("AI close error")
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        if self.mongo is not None:
            self.mongo.close()
            self.mongo = None
        await super().close()

    async def on_guild_join(self, guild):
        """При входе на новый сервер — создаём локальные ЦБ и госбюджет."""
        await self.ensure_guild_funds(guild.id)
        log.info("Новый сервер %s (%s) — создана локальная экономика", guild.name, guild.id)

    # ---------- ИЗОЛЯЦИЯ: создание локальных фондов ----------
    async def ensure_guild_funds(self, guild_id):
        """Создаёт (если нет) локальные документы ЦБ и госбюджета для сервера."""
        gid = int(guild_id)
        await self.economy.update_one(
            {"_id": self.cb_id(gid)},
            {"$setOnInsert": {"guild_id": gid, "reserve": 0, "printed": 0, "created_at": now()},
             "$set": {"updated_at": now()}},
            upsert=True,
        )
        await self.funds.update_one(
            {"_id": self.budget_id(gid)},
            {"$setOnInsert": {"guild_id": gid, "name": "Государственный бюджет",
                              "balance": 0, "created_at": now()},
             "$set": {"updated_at": now()}},
            upsert=True,
        )

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound): return
        if isinstance(error, commands.MissingPermissions):
            return await send_embed(ctx, "danger", "Нет прав",
                                    "Эта команда доступна только администраторам.")
        if isinstance(error, commands.NoPrivateMessage):
            return await send_embed(ctx, "danger", "Только на сервере",
                                    "Эта команда работает только на сервере.")
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument,
                              commands.BadUnionArgument)):
            return await send_embed(ctx, "danger", "Неверные аргументы",
                                    "Используй `!help` для списка команд.")
        if isinstance(error, commands.CheckFailure):
            return await send_embed(ctx, "danger", "Команда недоступна")
        log.error("Ошибка команды %s", ctx.command, exc_info=error)
        await send_embed(ctx, "danger", "Внутренняя ошибка",
                         "Попробуй ещё раз чуть позже.")

    # ---------- Миграция ----------
    async def migrate_legacy(self):
        raw = (os.getenv("LEGACY_GUILD_ID") or "").strip()
        legacy_gid = int(raw) if raw.isdigit() else None
        for name, coll in (("users", self.db_users), ("businesses", self.businesses),
                           ("casino_stats", self.casino_stats)):
            archive = self.db[f"{name}_legacy"]
            moved = 0
            async for doc in coll.find({"user_id": {"$exists": False}}):
                if legacy_gid is None:
                    try: await archive.insert_one(doc)
                    except DuplicateKeyError: pass
                    await coll.delete_one({"_id": doc["_id"]})
                else:
                    await self._import_legacy_doc(coll, doc, legacy_gid)
                    await coll.delete_one({"_id": doc["_id"]})
                moved += 1
            if moved and legacy_gid is None:
                log.warning("Миграция: %s старых %s -> %s_legacy", moved, name, name)

    @staticmethod
    async def _import_legacy_doc(coll, doc, guild_id):
        new = {k: v for k, v in doc.items() if k != "_id"}
        new["guild_id"] = int(guild_id)
        new["user_id"] = int(doc["_id"])
        try:
            await coll.insert_one(new)
            return True
        except DuplicateKeyError:
            return False

    # ---------- ИЗОЛЯЦИЯ АКЦИЙ: _id = "guild_id:ticker", guild_id обязателен ----------
    async def migrate_stock_isolation(self):
        """Приводит stocks/stock_holdings к строгой привязке к серверу (идемпотентно)."""
        try:
            fixed = archived = 0
            async for st in self.stocks.find({}):
                gid, ticker = st.get("guild_id"), st.get("ticker")
                if not isinstance(gid, int) or not ticker:
                    m = re.match(r"^(\d+):(.+)$", str(st["_id"]))
                    if not m:
                        try: await self.db["stocks_legacy"].insert_one(st)
                        except DuplicateKeyError: pass
                        await self.stocks.delete_one({"_id": st["_id"]})
                        archived += 1
                        continue
                    gid, ticker = int(m.group(1)), m.group(2)
                    await self.stocks.update_one({"_id": st["_id"]},
                                                 {"$set": {"guild_id": gid, "ticker": ticker}})
                want = f"{int(gid)}:{ticker}"
                if st["_id"] != want:
                    new = {**st, "_id": want, "guild_id": int(gid), "ticker": ticker}
                    try: await self.stocks.insert_one(new)
                    except DuplicateKeyError: pass
                    await self.stocks.delete_one({"_id": st["_id"]})
                    fixed += 1
            async for h in self.stock_holdings.find({"guild_id": {"$exists": False}}):
                try: await self.db["stock_holdings_legacy"].insert_one(h)
                except DuplicateKeyError: pass
                await self.stock_holdings.delete_one({"_id": h["_id"]})
                archived += 1
            if fixed or archived:
                log.warning("Миграция акций: исправлено %s, в архив %s", fixed, archived)
        except Exception:
            log.exception("Миграция изоляции акций не удалась")

    # ---------- Настройки ----------
    def invalidate_settings(self, guild_id):
        self._settings_cache.pop(int(guild_id), None)

    async def get_settings(self, guild_id) -> dict:
        gid = int(guild_id)
        cached = self._settings_cache.get(gid)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        doc = await self.settings.find_one({"_id": gid}) or {}
        eco = doc.get("eco") or {}
        cfg = dict(DEFAULT_SETTINGS)
        legacy_used = False
        for key in COOLDOWN_KEYS:
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
        # Старые документы без inflation_multiplier: берём значение из пресета гильдии
        if preset in PRESETS and "inflation_multiplier" not in eco:
            cfg["inflation_multiplier"] = PRESETS[preset]["values"]["inflation_multiplier"]
        cfg["inflation_multiplier"] = float(cfg["inflation_multiplier"])
        # Модули
        mods = doc.get("modules") or {}
        cfg["modules"] = {k: bool(mods.get(k, v)) for k, v in DEFAULT_MODULES.items()}
        # Кастомные тексты (!crime / !slut): пустое значение = стандартный текст
        raw_texts = doc.get("custom_texts") or {}
        cfg["custom_texts"] = {k: raw_texts[k].strip() for k in CUSTOM_TEXT_KEYS
                               if isinstance(raw_texts.get(k), str) and raw_texts[k].strip()}
        self._settings_cache[gid] = (time.monotonic() + 30, cfg)
        return dict(cfg)

    async def get_cooldown(self, guild_id, key: str) -> int:
        return int((await self.get_settings(guild_id))[f"cooldown_{key}"])

    async def module_enabled(self, guild_id, module: str) -> bool:
        cfg = await self.get_settings(guild_id)
        return bool(cfg.get("modules", {}).get(module, True))

    # ---------- ИЗОЛЯЦИЯ: работа с локальными фондами ----------
    async def ensure_user(self, guild_id: int, user_id: int, *, session=None):
        flt = ukey(guild_id, user_id)
        try:
            await self.db_users.update_one(
                flt, {"$setOnInsert": {
                    "cash": 0, "bank": 0, "debt": 0,
                    "last_work": None, "last_crime": None, "last_slut": None,
                    "last_rob": None, "last_daily": None, "created_at": now()}},
                upsert=True, session=session,
            )
        except DuplicateKeyError:
            pass
        return await self.db_users.find_one(flt, session=session)

    async def get_balance(self, guild_id: int, user_id: int):
        return await self.ensure_user(guild_id, user_id)

    async def change_cash(self, guild_id: int, user_id: int, delta: int, *, session=None):
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

    async def journal(self, source, destination, amount, reason, meta=None, *,
                      session=None, guild_id=None):
        doc = {"source": str(source), "destination": str(destination),
               "amount": int(amount), "reason": reason,
               "meta": meta or {}, "created_at": now()}
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
            async with await self.mongo.start_session() as session:
                async with session.start_transaction():
                    sender = await self.db_users.find_one(ukey(guild_id, sender_id), session=session)
                    if int(sender.get("debt", 0)) > 0:
                        raise EconomyError("У тебя есть долг. Погаси его перед переводами.")
                    if int(sender.get("cash", 0)) < amount:
                        raise EconomyError("Недостаточно наличных.")
                    debited = await self.db_users.update_one(
                        {**ukey(guild_id, sender_id), "cash": {"$gte": amount}, "debt": 0},
                        {"$inc": {"cash": -amount}, "$set": {"updated_at": now()}}, session=session)
                    if debited.modified_count != 1:
                        raise EconomyError("Перевод отклонён.")
                    await self.db_users.update_one(
                        ukey(guild_id, recipient_id),
                        {"$inc": {"cash": amount}, "$set": {"updated_at": now()}}, session=session)
                    await self.journal(f"user_{sender_id}", f"user_{recipient_id}", amount,
                                       "Перевод между игроками",
                                       {"type": "player_transfer"}, session=session, guild_id=guild_id)
        except EconomyError as e:
            return False, str(e)
        return True, "Перевод выполнен."

    async def claim_cooldown(self, guild_id, user_id, key, cooldown, *, session) -> bool:
        field = f"last_{key}"
        current = now()
        threshold = current - timedelta(seconds=int(cooldown))
        result = await self.db_users.update_one(
            {**ukey(guild_id, user_id), "$or": [{field: None}, {field: {"$lte": threshold}}]},
            {"$set": {field: current, "updated_at": current}}, session=session)
        return result.matched_count == 1

    async def cooldown_left(self, guild_id, user_id, key, cooldown) -> float:
        doc = await self.db_users.find_one(ukey(guild_id, user_id), {f"last_{key}": 1}) or {}
        last = as_utc(doc.get(f"last_{key}"))
        if last is None: return 0
        return max(0, cooldown - (now() - last).total_seconds())

    # ---------- ИЗОЛЯЦИЯ: локальные резерв и бюджет ----------
    async def reserve(self, guild_id, *, session=None):
        doc = await self.economy.find_one({"_id": self.cb_id(guild_id)}, session=session)
        return int((doc or {}).get("reserve", 0))

    async def change_reserve(self, guild_id, delta: int, *, session=None, allow_negative=False):
        delta = int(delta)
        cb_id = self.cb_id(guild_id)
        if delta < 0 and not allow_negative:
            result = await self.economy.update_one(
                {"_id": cb_id, "reserve": {"$gte": -delta}},
                {"$inc": {"reserve": delta}, "$set": {"updated_at": now()}},
                session=session)
        else:
            result = await self.economy.update_one(
                {"_id": cb_id},
                {"$inc": {"reserve": delta}, "$set": {"updated_at": now()}},
                upsert=True, session=session)
        return result.modified_count == 1

    async def fund_balance(self, guild_id):
        doc = await self.funds.find_one({"_id": self.budget_id(guild_id)})
        return int((doc or {}).get("balance", 0))

    async def add_budget(self, guild_id, amount: int, reason: str, *, session=None, source="taxpayer"):
        amount = int(amount)
        if amount <= 0: return
        budget_id = self.budget_id(guild_id)
        await self.funds.update_one(
            {"_id": budget_id},
            {"$inc": {"balance": amount}, "$set": {"updated_at": now()},
             "$setOnInsert": {"guild_id": int(guild_id), "name": "Государственный бюджет",
                              "created_at": now()}},
            upsert=True, session=session)
        await self.journal(source, f"fund_{budget_id}", amount, reason,
                           session=session, guild_id=guild_id)

    async def _pay_earning_tx(self, session, guild_id, user_id, gross, tax, net, reason, account, tax_rate):
        """Заработок = перевод из Резерва ЦБ: reserve -gross, игрок +net, госбюджет +tax.
        Денежная масса не меняется, `printed` НЕ растёт (деньги напечатаны ранее через !print-money).
        Всё выполняется в транзакции вызывающего: если резерва не хватает — бросаем исключение,
        транзакция откатывается целиком (в т.ч. кулдаун, если его взял вызывающий)."""
        if gross > 0:
            # атомарное условное списание: {reserve: {$gte: gross}} -> иначе modified_count == 0
            if not await self.change_reserve(guild_id, -gross, session=session):
                raise TreasuryEmptyError(TREASURY_EMPTY_TEXT)
        await self.db_users.update_one(
            ukey(guild_id, user_id),
            {"$inc": {account: net}, "$set": {"updated_at": now()}}, session=session)
        await self.add_budget(guild_id, tax, f"НДФЛ {fmt_pct(tax_rate)}: {reason}",
                              session=session, source="central_bank")
        await self.journal("central_bank", f"user_{user_id}", net, reason,
                           {"gross": gross, "tax": tax, "tax_rate": tax_rate, "type": "earning"},
                           session=session, guild_id=guild_id)

    async def pay_earning(self, guild_id, user_id, gross, reason, *, tax_rate,
                          account="cash", session=None):
        """Выплата заработка из Резерва ЦБ. Возвращает (gross, tax, net).
        Бросает TreasuryEmptyError, если резерва недостаточно.
        Передай session, чтобы выплата и, например, кулдаун были ОДНОЙ транзакцией."""
        gross = max(0, int(gross))
        tax = int(gross * float(tax_rate))
        net = gross - tax
        await self.ensure_user(guild_id, user_id, session=session)
        if session is not None:
            await self._pay_earning_tx(session, guild_id, user_id, gross, tax, net,
                                       reason, account, tax_rate)
        else:
            async with await self.mongo.start_session() as own_session:
                async with own_session.start_transaction():
                    await self._pay_earning_tx(own_session, guild_id, user_id, gross, tax, net,
                                               reason, account, tax_rate)
        return gross, tax, net

    async def stats(self, guild_id):
        """Статистика ТОЛЬКО по указанному серверу."""
        gid = int(guild_id)
        await self.ensure_guild_funds(gid)
        reserve = await self.reserve(gid)
        budget = await self.fund_balance(gid)
        players_rows = await self.db_users.aggregate([
            {"$match": {"guild_id": gid}},
            {"$group": {"_id": None, "cash": {"$sum": "$cash"},
                        "bank": {"$sum": "$bank"}, "debt": {"$sum": "$debt"}}},
        ]).to_list(1)
        p = players_rows[0] if players_rows else {}
        players = int(p.get("cash", 0)) + int(p.get("bank", 0))
        player_debt = int(p.get("debt", 0))
        since = now() - timedelta(hours=24)
        incoming = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "guild_id": gid,
                        "destination": {"$regex": "^user_"}}},
            {"$group": {"_id": None, "n": {"$sum": "$amount"}}},
        ]).to_list(1)
        outgoing = await self.tx.aggregate([
            {"$match": {"created_at": {"$gte": since}, "guild_id": gid,
                        "source": {"$regex": "^user_"}}},
            {"$group": {"_id": None, "n": {"$sum": "$amount"}}},
        ]).to_list(1)
        printed = int((await self.economy.find_one({"_id": self.cb_id(gid)}) or {}).get("printed", 0))
        supply = reserve + players + budget
        printed_24h, burned_24h = await self.emission_24h(gid)
        return {
            "reserve": reserve, "budget": budget,
            "players": players, "player_debt": player_debt,
            "circulation": players + budget,
            "debt": max(0, -reserve), "supply": supply,
            "printed": printed,
            "printed_24h": printed_24h, "burned_24h": burned_24h,
            "inflation": self.calc_inflation(printed_24h - burned_24h, supply),
            "incoming": int(incoming[0]["n"]) if incoming else 0,
            "outgoing": int(outgoing[0]["n"]) if outgoing else 0,
        }

    @staticmethod
    def calc_inflation(net_emission_24h, supply) -> float:
        """Инфляция, % = чистая эмиссия за 24ч / денежная масса * 100.

        net_emission_24h = напечатано за 24ч (print-money, заработок, доход бизнеса)
                           минус сожжено за 24ч (burn-money).
        Знаменатель не меньше самой эмиссии, поэтому значение ограничено 100%
        и нет деления на ноль при пустой/отрицательной денежной массе.
        Дефляции нет: при чистом сжигании инфляция просто падает до 0.
        """
        net = int(net_emission_24h)
        if net <= 0:
            return 0.0
        denom = max(int(supply), net, 1)
        return net / denom * 100.0

    INFLATION_CACHE_TTL = 10  # секунд

    async def emission_24h(self, guild_id):
        """(напечатано, сожжено) за последние 24 часа по журналу транзакций сервера."""
        since = now() - timedelta(hours=24)
        rows = await self.tx.aggregate([
            {"$match": {"guild_id": int(guild_id), "created_at": {"$gte": since},
                        "$or": [{"source": "money_printing"}, {"destination": "money_burn"}]}},
            {"$group": {"_id": None,
                        "printed": {"$sum": {"$cond": [{"$eq": ["$source", "money_printing"]}, "$amount", 0]}},
                        "burned": {"$sum": {"$cond": [{"$eq": ["$destination", "money_burn"]}, "$amount", 0]}}}},
        ]).to_list(1)
        if not rows:
            return 0, 0
        return int(rows[0].get("printed", 0)), int(rows[0].get("burned", 0))

    def invalidate_inflation(self, guild_id):
        self._infl_cache.pop(int(guild_id), None)

    @staticmethod
    def inflation_factor(inflation, multiplier) -> float:
        return max(0.0, 1.0 - float(inflation) * 0.01 * float(multiplier))

    async def current_inflation(self, guild_id) -> float:
        """Инфляция сервера (кэш 10 с; сбрасывается при print/burn/заработке)."""
        gid = int(guild_id)
        cached = self._infl_cache.get(gid)
        if cached and time.monotonic() - cached[0] < self.INFLATION_CACHE_TTL:
            return cached[1]
        reserve = await self.reserve(gid)
        budget = await self.fund_balance(gid)
        rows = await self.db_users.aggregate([
            {"$match": {"guild_id": gid}},
            {"$group": {"_id": None, "cash": {"$sum": "$cash"}, "bank": {"$sum": "$bank"}}},
        ]).to_list(1)
        p = rows[0] if rows else {}
        players = int(p.get("cash", 0)) + int(p.get("bank", 0))
        printed_24h, burned_24h = await self.emission_24h(gid)
        value = self.calc_inflation(printed_24h - burned_24h, reserve + players + budget)
        self._infl_cache[gid] = (time.monotonic(), value)
        return value

    async def snapshot(self):
        """Делаем снапшоты по всем серверам."""
        for guild in self.guilds:
            try:
                s = await self.stats(guild.id)
                await self.history.insert_one({
                    "guild_id": guild.id, "created_at": now(), **s,
                    "state_budget": s["budget"],
                })
            except Exception:
                log.exception("Snapshot failed for guild %s", guild.id)

    @staticmethod
    def _render_chart(rows, path, value_key="circulation", decimals=0, xfmt="%H:%M"):
        """Тёмный график «в стиле Discord»: зелёная линия + полупрозрачная заливка."""
        bg, fg, grid, green = "#2f3136", "#b9bbbe", "#ffffff", "#2ecc71"
        dates = [as_utc(r["created_at"]) for r in rows]
        values = [float(r.get(value_key, 0)) for r in rows]
        fig, ax = plt.subplots(figsize=(9, 4.5))
        fig.patch.set_facecolor(bg)
        ax.set_facecolor(bg)
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.1 or max(1, abs(hi) * 0.05)
        floor = lo - pad
        ax.set_ylim(floor, hi + pad)
        ax.plot(dates, values, color=green, linewidth=2,
                marker="o" if len(values) <= 60 else None, markersize=3)
        ax.fill_between(dates, values, floor, color=green, alpha=0.15)
        ax.grid(True, color=grid, alpha=0.06)
        for side in ("top", "right", "left", "bottom"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=fg, length=0, labelsize=9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter(xfmt))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax.yaxis.set_major_formatter(FuncFormatter(
            (lambda v, _pos: fmt(v)) if decimals == 0
            else (lambda v, _pos: f"{v:,.{decimals}f}".replace(",", " "))))
        fig.tight_layout()
        fig.savefig(path, dpi=120, facecolor=bg)
        plt.close(fig)

    async def publish_charts(self, guild_id, channel=None) -> bool:
        async with self.chart_lock:
            rows = await self.history.find({"guild_id": int(guild_id)}).sort("created_at", 1).to_list(100000)
            if not rows: return False
            clean = [r for r in rows if as_utc(r.get("created_at"))]
            if not clean: return False
            day = [r for r in clean if as_utc(r["created_at"]) >= now() - timedelta(hours=24)] or clean[-1:]
            try:
                await asyncio.to_thread(self._render_chart, clean[-2500:], self.chart_files["all"])
                await asyncio.to_thread(self._render_chart, day[-1500:], self.chart_files["24h"])
            except Exception:
                log.exception("Chart render failed")
                return False
            if channel is None: return False
            try:
                files = [discord.File(self.chart_files["24h"], filename="economy_chart.png"),
                         discord.File(self.chart_files["all"], filename="economy_chart_all.png")]
            except OSError:
                return False
            current = int(clean[-1].get("circulation", 0))
            first_day = int(day[0].get("circulation", 0))
            diff = current - first_day
            e24 = make_embed("info", "Денежная масса — 24 часа",
                description=(f"В обращении: `{fmt(current)}`\n"
                             f"Изменение за период: `{'+' if diff >= 0 else '-'}{fmt(abs(diff))}`"),
                footer=f"Сервер ID: {int(guild_id)}")
            e24.set_image(url="attachment://economy_chart.png")
            eall = make_embed("info", "Денежная масса — вся история")
            eall.set_image(url="attachment://economy_chart_all.png")
            message = await safe_send(channel, embeds=[e24, eall], files=files)
            return message is not None

    # ---------- Фоновые задачи ----------
    @tasks.loop(hours=1)
    async def business_loop(self):
        current = now()
        async for business in self.businesses.find({}):
            last = as_utc(business.get("last_payout"))
            if last and (current - last).total_seconds() < 3600: continue
            gid, uid = business.get("guild_id"), business.get("user_id")
            if gid is None or uid is None: continue
            gid, uid = int(gid), int(uid)
            # Модуль выключен — бизнесы сервера не трогаем (данные сохраняются)
            if not await self.module_enabled(gid, "business"):
                continue
            level = max(1, int(business.get("level", 1)))
            try:
                cfg = await self.get_settings(gid)
                income = business_income(level)
                infl_mult = float(cfg.get("inflation_multiplier", 0.0))
                if infl_mult > 0:
                    inflation = await self.current_inflation(gid)
                    income = int(income * self.inflation_factor(inflation, infl_mult))
                tax = business_tax(level, cfg["business_tax"])
                async with await self.mongo.start_session() as session:
                    async with session.start_transaction():
                        await self.ensure_user(gid, uid, session=session)
                        debit = await self.db_users.update_one(
                            {**ukey(gid, uid), "cash": {"$gte": tax}},
                            {"$inc": {"cash": -tax, "bank": income}, "$set": {"updated_at": current}},
                            session=session)
                        if debit.modified_count != 1:
                            await self.businesses.update_one(
                                ukey(gid, uid),
                                {"$set": {"frozen": True, "last_checked": current}}, session=session)
                            continue
                        await self.add_budget(gid, tax,
                            f"Налог на имущество бизнеса {fmt_pct(cfg['business_tax'])}",
                            session=session)
                        # Доход бизнеса создаётся из воздуха — учитываем как эмиссию
                        await self.economy.update_one(
                            {"_id": self.cb_id(gid)},
                            {"$inc": {"printed": income}, "$set": {"updated_at": current},
                             "$setOnInsert": {"guild_id": gid, "reserve": 0, "created_at": current}},
                            upsert=True, session=session)
                        await self.journal("money_printing", "economy", income,
                                           "Эмиссия: доход бизнеса", {"type": "business_emission"},
                                           session=session, guild_id=gid)
                        await self.businesses.update_one(
                            ukey(gid, uid),
                            {"$set": {"last_payout": current, "last_checked": current, "frozen": False}},
                            session=session)
                        await self.journal(f"business_{uid}", f"user_{uid}", income,
                                           "Доход бизнеса", {"property_tax": tax, "level": level},
                                           session=session, guild_id=gid)
            except Exception:
                log.exception("Business payout failed guild=%s uid=%s", gid, uid)

    @business_loop.before_loop
    async def before_business_loop(self): await self.wait_until_ready()

    @tasks.loop(hours=1)
    async def loan_loop(self):
        current = now()
        async for user in self.db_users.find({"debt": {"$gt": 0}}):
            gid, uid = user.get("guild_id"), user.get("user_id")
            if gid is None or uid is None: continue
            gid, uid = int(gid), int(uid)
            try:
                due = as_utc(user.get("loan_due"))
                if due is None:
                    await self.db_users.update_one(
                        {**ukey(gid, uid), "debt": {"$gt": 0}, "loan_due": None},
                        {"$set": {"loan_due": current + timedelta(seconds=LOAN_TERM)}})
                    continue
                if current <= due: continue
                last = as_utc(user.get("loan_penalty_at")) or due
                periods = min(int((current - last).total_seconds() // 86400), 30)
                if periods < 1: continue
                rate = (await self.get_settings(gid))["loan_penalty"]
                debt = int(user["debt"])
                new_debt = debt
                for _ in range(periods):
                    new_debt += int(new_debt * rate)
                result = await self.db_users.update_one(
                    {**ukey(gid, uid), "debt": debt},
                    {"$set": {"debt": new_debt, "loan_penalty_at": last + timedelta(days=periods),
                              "updated_at": current}})
                if result.modified_count == 1 and new_debt != debt:
                    log.info("Loan penalty guild=%s uid=%s %s -> %s", gid, uid, debt, new_debt)
            except Exception:
                log.exception("Loan penalty failed guild=%s uid=%s", gid, uid)

    @loan_loop.before_loop
    async def before_loan_loop(self): await self.wait_until_ready()

    @tasks.loop(minutes=STOCK_TICK_MINUTES)
    async def stock_loop(self):
        """Раз в 30 минут: случайное движение цен на ±3% + запись в history."""
        current = now()
        async for st in self.stocks.find({}):
            try:
                gid = int(st.get("guild_id", 0))
                if not gid or not await self.module_enabled(gid, "stocks"):
                    continue
                last = as_utc(st.get("last_tick"))
                # защита от лишнего тика при перезапуске бота
                if last and (current - last).total_seconds() < (STOCK_TICK_MINUTES - 1) * 60:
                    continue
                for _attempt in range(3):
                    pct = rng.uniform(-STOCK_TICK_MAX, STOCK_TICK_MAX)
                    price = max(1.0, round(float(st["price"]) * (1 + pct), 2))
                    res = await self.stocks.update_one(
                        {"_id": st["_id"], "price": st["price"]},   # не затираем параллельную сделку
                        {"$set": {"price": price, "last_tick": current, "updated_at": current},
                         "$push": {"history": {"$each": [price], "$slice": -STOCK_HISTORY_MAX},
                                   "history_ts": {"$each": [current], "$slice": -STOCK_HISTORY_MAX}}})
                    if res.modified_count == 1:
                        break
                    st = await self.stocks.find_one({"_id": st["_id"]})
                    if not st:
                        break
            except Exception:
                log.exception("Stock tick failed %s", st.get("_id") if st else None)

    @stock_loop.before_loop
    async def before_stock_loop(self): await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def snapshot_loop(self):
        try: await self.snapshot()
        except Exception: log.exception("Snapshot loop failed")

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self): await self.wait_until_ready()

    async def _resolve_channel(self, guild, channel_id):
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try: channel = await self.fetch_channel(int(channel_id))
            except Exception: channel = None
        return channel

    @tasks.loop(minutes=5)
    async def daily_report_loop(self):
        current = now()
        if current.hour != REPORT_HOUR_UTC: return
        date_key = current.strftime("%Y-%m-%d")
        query = {"$or": [{"report_channel_id": {"$exists": True}},
                          {"chart_channel_id": {"$exists": True}}]}
        async for config in self.settings.find(query):
            if config.get("last_automation_date") == date_key: continue
            guild = self.get_guild(int(config["_id"]))
            if guild is None: continue
            try:
                stats = await self.stats(int(config["_id"]))
                report_channel_id = config.get("report_channel_id")
                if report_channel_id:
                    channel = await self._resolve_channel(guild, report_channel_id)
                    if channel is not None:
                        embed = make_embed("info", "Ежедневный экономический отчёт",
                            fields=[
                                ("🏦 Резерв ЦБ", f"`{fmt(stats['reserve'])}`", True),
                                ("💰 Госбюджет", f"`{fmt(stats['budget'])}`", True),
                                ("👥 Средства игроков", f"`{fmt(stats['players'])}`", True),
                                ("🪙 В обращении", f"`{fmt(stats['circulation'])}`", True),
                                ("📉 Долги игроков", f"`{fmt(stats['player_debt'])}`", True),
                                ("↗️ Приход 24ч", f"`{fmt(stats['incoming'])}`", True),
                                ("↘️ Расход 24ч", f"`{fmt(stats['outgoing'])}`", True),
                            ],
                            footer=f"Дата отчёта: {date_key} UTC")
                        await safe_send(channel, embed=embed)
                chart_channel_id = config.get("chart_channel_id")
                if chart_channel_id:
                    chart_channel = await self._resolve_channel(guild, chart_channel_id)
                    if chart_channel is not None:
                        await self.snapshot()
                        await self.publish_charts(guild.id, channel=chart_channel)
                await self.settings.update_one(
                    {"_id": int(config["_id"])},
                    {"$set": {"last_automation_date": date_key}})
            except Exception:
                log.exception("Daily report failed guild %s", config.get("_id"))

    @daily_report_loop.before_loop
    async def before_daily_report_loop(self): await self.wait_until_ready()


# ====================== КРИМИНАЛ-ТЕКСТЫ ======================
CRIME_ACTIONS = {
    "crime": {
        "title": "🕵️ Кража", "reward": CRIME_REWARD,
        "win": ["Ты тихо вскрыл склад и ушёл незамеченным.",
                "Охрана смотрела футбол — ты вынес всё, что плохо лежало.",
                "Карманная кража в метро прошла идеально."],
        "lose": ["Тебя поймала охрана и вызвала полицию.",
                 "Сработала сигнализация — пришлось бежать, но тебя опознали.",
                 "Жертва оказалась переодетым полицейским."],
        "wait": "Полиция ещё ищет тебя после прошлого дела.",
        "reason_win": "Кража: добыча", "reason_fine": "Штраф за неудачную кражу",
    },
    "slut": {
        "title": "💋 Ночная подработка", "reward": SLUT_REWARD,
        "win": ["Клиент оказался щедрым и оставил хорошие чаевые.",
                "Вечер удался — смена принесла отличный заработок.",
                "VIP-гость заплатил вдвое больше обычного."],
        "lose": ["Нагрянула облава — пришлось платить штраф.",
                 "Клиент сбежал, не заплатив, а тебя ещё и оштрафовали.",
                 "Конкуренты вызвали проверку, и ты попал под штраф."],
        "wait": "Ты ещё не отдохнул после прошлой смены.",
        "reason_win": "Ночная подработка: доход",
        "reason_fine": "Штраф за неудачную ночную подработку",
    },
}


# ====================== ECONOMY COG ======================
class EconomyCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot

    # ---------- Проверка модуля ----------
    async def _require_module(self, ctx, module: str, title: str) -> bool:
        if await self.bot.module_enabled(ctx.guild.id, module):
            return True
        await send_embed(ctx, "warning", f"{title} отключён",
                         "Администратор сервера выключил этот модуль. "
                         "Включить можно через `/settings`.")
        return False

    async def _inflation_fields(self, guild_id, s):
        cfg = await self.bot.get_settings(guild_id)
        if cfg.get("preset") == "easy":
            return [("📈 Инфляция", "`отключена`", True)]
        return [("📈 Инфляция", f"`{s['inflation']:.2f}%`", True)]

    @commands.hybrid_command(name="help", aliases=["h"], description="Команды экономики")
    async def help_command(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        await send_embed(ctx, "info", "Справка по экономике",
            description="**Баланс у каждого сервера свой — своя инфляция, свой ЦБ.**",
            fields=[
                ("💰 Баланс и переводы",
                 "`!balance` — баланс\n`!pay @user сумма` — перевод", False),
                ("💼 Заработок",
                 "`!work` `!crime` `!slut` `!daily` — заработок\n`!rob @user` — ограбить", False),
                ("🏦 Банк",
                 "`!deposit сумма|all` — в банк\n`!withdraw сумма|all` — снять", False),
                ("🏢 Бизнес",
                 "`!business buy` — купить\n`!business status` — статус\n`!business upgrade` — улучшить", False),
                ("💳 Кредит",
                 "`!loan take сумма` — взять\n`!loan pay сумма` — погасить", False),
                ("📊 Статистика",
                 "`!top` — топ-10\n`!economy` — экономика сервера\n`!cb` — ЦБ сервера", False),
                ("🎰 Казино",
                 "`!slot ставка` `!bj ставка`\n`!rr ставка` — русская рулетка (соло / с друзьями)\n`!casinostats [@user]`", False),
                ("📈 Биржа",
                 "`!stock create [ТИКЕР] Название` — IPO (100 000), тикер необязателен\n`!stock info ТИКЕР` — профиль и графики\n"
                 "`!stock list` — все акции (кнопки — топ-5 по цене)\n`!stock my` — мои акции и компании\n"
                 "`!buy ТИКЕР кол-во` `!sell ТИКЕР кол-во|all`", False),
                ("⚙️ Админ",
                 "`/settings` — панель (в т.ч. ✏️ кастомные тексты)\n`!graphs` — графики\n🤖 ИИ — кнопки в `/settings`", False),
            ],
            thumbnail=ctx.guild.icon.url if ctx.guild.icon else None)

    @commands.hybrid_command(name="balance", aliases=["bal", "b"], description="Показать баланс")
    async def balance(self, ctx, member: discord.Member = None):
        async with ctx.typing():
            await self._balance(ctx, member)

    async def _balance(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        data = await self.bot.get_balance(ctx.guild.id, member.id)
        await send_embed(ctx, "info", f"Баланс — {member.display_name}",
            fields=[
                ("💵 Наличные", f"`{fmt(data['cash'])}`", True),
                ("🏦 Банк", f"`{fmt(data['bank'])}`", True),
                ("💳 Долг", f"`{fmt(data['debt'])}`", True),
            ],
            thumbnail=member.display_avatar.url,
            footer=f"Сервер: {ctx.guild.name}")

    @commands.hybrid_command(name="pay", aliases=["p"], description="Перевести наличные игроку")
    @app_commands.describe(member="Кому", amount="Сумма")
    async def pay(self, ctx, member: discord.Member, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if member.bot:
            return await send_embed(ctx, "danger", "Ошибка", "Нельзя переводить ботам.")
        ok, message = await self.bot.transfer_players(ctx.guild.id, ctx.author.id, member.id, amount)
        await send_embed(ctx, "success" if ok else "danger",
                         "Перевод выполнен" if ok else "Перевод отклонён", message)

    @commands.hybrid_command(name="work", aliases=["w"], description="Поработать")
    async def work(self, ctx):
        async with ctx.typing():
            await self._work(ctx)

    async def _work(self, ctx):
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        cooldown = cfg["cooldown_work"]
        low, high = sorted((cfg["work_min"], cfg["work_max"]))
        gross = random.randint(low, high)
        infl_cut = 0.0
        infl_mult = float(cfg.get("inflation_multiplier", 0.0))
        if infl_mult > 0:
            inflation = await self.bot.current_inflation(gid)
            factor = self.bot.inflation_factor(inflation, infl_mult)
            gross = int(gross * factor)
            infl_cut = (1 - factor) * 100
        try:
            # Одна транзакция: кулдаун + списание из резерва + НДФЛ в бюджет + зачисление игроку.
            # Любое исключение (в т.ч. пустая казна) откатывает всё — кулдаун не тратится.
            # Резерв — «горячий» документ: при write-conflict транзакцию повторяем.
            for attempt in range(3):
                try:
                    async with await self.bot.mongo.start_session() as session:
                        async with session.start_transaction():
                            if not await self.bot.claim_cooldown(gid, uid, "work", cooldown, session=session):
                                raise CooldownError("cooldown")
                            _, tax, net = await self.bot.pay_earning(
                                gid, uid, gross, "Зарплата за работу",
                                tax_rate=cfg["tax_rate"], session=session)
                    break
                except OperationFailure as e:
                    if attempt < 2 and e.has_error_label("TransientTransactionError"):
                        await asyncio.sleep(0.05 * (attempt + 1))
                        continue
                    raise
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "work", cooldown)
            return await send_embed(ctx, "warning", "Ты устал",
                                    f"Следующая работа через **{fmt_duration(remaining)}**.")
        except TreasuryEmptyError as e:
            return await send_embed(ctx, "danger", "🏦 Казна пуста", str(e))
        fields = [("💰 Начислено", f"`{fmt(gross)}`", True)]
        if infl_cut > 0:
            fields.append(("📉 Инфляция", f"`-{infl_cut:.1f}%` к зарплате", True))
        if tax > 0:
            fields.append(("🧾 НДФЛ", f"`{fmt(tax)}` ({fmt_pct(cfg['tax_rate'])})", True))
        fields.append(("✅ На руки", f"`{fmt(net)}`", True))
        await send_embed(ctx, "success", "Зарплата получена", fields=fields,
                         thumbnail=ctx.author.display_avatar.url)

    async def _criminal_action(self, ctx, key: str):
        if not await self._require_module(ctx, "crime", "Криминал"): return
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
                        # Добыча выплачивается из резерва ЦБ (reserve -> cash): масса не растёт,
                        # printed не трогаем — иначе эмиссия была бы посчитана дважды.
                        if not await self.bot.change_reserve(gid, -reward, session=session):
                            raise EconomyError("В резерве ЦБ недостаточно средств. Попроси админа сделать эмиссию.")
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
                                {**ukey(gid, uid), "cash": {"$gte": from_cash},
                                 "bank": {"$gte": from_bank}},
                                {"$inc": {"cash": -from_cash, "bank": -from_bank},
                                 "$set": {"updated_at": now()}}, session=session)
                            if debited.modified_count != 1:
                                raise EconomyError("Баланс изменился. Попробуй ещё раз.")
                            await self.bot.add_budget(gid, fine, cfg_action["reason_fine"],
                                                      session=session, source=f"user_{uid}")
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, key, cooldown)
            return await send_embed(ctx, "warning", cfg_action["title"],
                                    f"{cfg_action['wait']}\nПопробуй через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await send_embed(ctx, "danger", cfg_action["title"], str(e))
        text_key = f"{key}_{'win' if success else 'lose'}"
        text = (cfg.get("custom_texts") or {}).get(text_key) or rng.choice(cfg_action["win" if success else "lose"])
        if success:
            await send_embed(ctx, "success", cfg_action["title"],
                description=text,
                fields=[("💰 Добыча", f"`{fmt(reward)}` (без налога)", False)],
                thumbnail=ctx.author.display_avatar.url)
        elif fine > 0:
            await send_embed(ctx, "danger", cfg_action["title"],
                description=text,
                fields=[("🚔 Штраф", f"`{fmt(fine)}` — уходит в госбюджет", False)])
        else:
            await send_embed(ctx, "danger", cfg_action["title"],
                description=text + "\n🚔 У тебя нечего отбирать — штраф 0.")

    @commands.hybrid_command(name="crime", aliases=["c"], description="Кража")
    async def crime(self, ctx):
        async with ctx.typing():
            await self._criminal_action(ctx, "crime")

    @commands.hybrid_command(name="slut", aliases=["s"], description="Ночная подработка")
    async def slut(self, ctx):
        async with ctx.typing():
            await self._criminal_action(ctx, "slut")

    @commands.hybrid_command(name="daily", aliases=["d"], description="Ежедневный бонус")
    async def daily(self, ctx):
        async with ctx.typing():
            await self._daily(ctx)

    async def _daily(self, ctx):
        if not await self._require_module(ctx, "daily", "Ежедневный бонус"): return
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
                        if not await self.bot.change_reserve(gid, -reward, session=session):
                            raise EconomyError("В резерве ЦБ недостаточно средств.")
                        await self.bot.change_cash(gid, uid, reward, session=session)
                        await self.bot.journal("central_bank", f"user_{uid}", reward,
                                               "Ежедневный бонус", {"type": "daily"},
                                               session=session, guild_id=gid)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "daily", cooldown)
            return await send_embed(ctx, "warning", "Бонус уже получен",
                                    f"Следующий через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        await send_embed(ctx, "success", "Ежедневный бонус",
            description=f"🎁 Получено **{fmt(reward)}** монет (без налога).",
            thumbnail=ctx.author.display_avatar.url)

    @commands.hybrid_command(name="rob", aliases=["r"], description="Ограбить игрока")
    @app_commands.describe(member="Кого грабим")
    async def rob(self, ctx, member: discord.Member):
        async with ctx.typing():
            await self._rob(ctx, member)

    async def _rob(self, ctx, member: discord.Member):
        if not await self._require_module(ctx, "crime", "Криминал"): return
        gid, uid, tid = ctx.guild.id, ctx.author.id, member.id
        if member.bot or tid == uid:
            return await send_embed(ctx, "danger", "Ошибка", "Нельзя грабить себя или ботов.")
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
                        raise EconomyError(f"У {member.display_name} меньше {fmt(ROB_MIN_TARGET_CASH)} наличных.")
                    robbed_at = as_utc(victim.get("last_robbed"))
                    if robbed_at and (now() - robbed_at).total_seconds() < ROB_VICTIM_PROTECTION:
                        left = ROB_VICTIM_PROTECTION - (now() - robbed_at).total_seconds()
                        raise EconomyError(f"{member.display_name} недавно ограбили. Защита ещё {fmt_duration(left)}.")
                    if success:
                        amount = max(1, min(int(vcash * loot_rate), ROB_MAX_LOOT))
                        taken = await self.bot.db_users.update_one(
                            {**ukey(gid, tid), "cash": {"$gte": amount}},
                            {"$inc": {"cash": -amount}, "$set": {"last_robbed": now(), "updated_at": now()}},
                            session=session)
                        if taken.modified_count != 1:
                            raise EconomyError("Баланс жертвы изменился.")
                        await self.bot.change_cash(gid, uid, amount, session=session)
                        await self.bot.journal(f"user_{tid}", f"user_{uid}", amount,
                                               "Ограбление", {"type": "rob"},
                                               session=session, guild_id=gid)
                    else:
                        thief = await self.bot.db_users.find_one(ukey(gid, uid), session=session) or {}
                        amount = min(int(int(thief.get("cash", 0)) * ROB_FINE_RATE), ROB_FINE_MAX)
                        if amount > 0:
                            if not await self.bot.change_cash(gid, uid, -amount, session=session):
                                raise EconomyError("Баланс изменился.")
                            await self.bot.change_cash(gid, tid, amount, session=session)
                            await self.bot.journal(f"user_{uid}", f"user_{tid}", amount,
                                                   "Штраф за неудачное ограбление",
                                                   {"type": "rob_fail"}, session=session, guild_id=gid)
        except CooldownError:
            remaining = await self.bot.cooldown_left(gid, uid, "rob", cooldown)
            return await send_embed(ctx, "warning", "Копы ещё рядом",
                                    f"Попробуй через **{fmt_duration(remaining)}**.")
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ограбление сорвалось", str(e))
        if success:
            await send_embed(ctx, "success", "🥷 Ограбление удалось",
                description=f"Ты украл у {member.mention} **{fmt(amount)}** монет.",
                thumbnail=ctx.author.display_avatar.url)
        elif amount > 0:
            await send_embed(ctx, "danger", "🚔 Тебя поймали",
                description=f"Ты выплатил {member.mention} компенсацию: **{fmt(amount)}**.")
        else:
            await send_embed(ctx, "danger", "🚔 Тебя поймали",
                description="С тебя нечего взять — штраф 0.")

    @commands.hybrid_command(name="set-cooldown", with_app_command=False, description="Кулдаун команды (админ)")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def setcooldown(self, ctx, command: Literal["work", "crime", "slut", "daily", "rob"], seconds: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if seconds < 0 or seconds > COOLDOWN_MAX:
            return await send_embed(ctx, "danger", "Ошибка",
                                    f"Кулдаун должен быть от 0 до {fmt(COOLDOWN_MAX)} секунд.")
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {f"eco.cooldown_{command}": int(seconds), "eco.preset": "custom",
                      "updated_at": now()}}, upsert=True)
        self.bot.invalidate_settings(ctx.guild.id)
        text = "без кулдауна" if seconds == 0 else fmt_duration(seconds)
        await send_embed(ctx, "success", "Кулдаун обновлён",
                         f"`{command}` теперь: **{text}** (пресет: ⚙️ Кастомная).")

    async def _move_money(self, ctx, raw_amount, *, to_bank: bool):
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        src = "cash" if to_bank else "bank"
        amount = parse_amount(raw_amount, int(user.get(src, 0)))
        if not amount:
            return await send_embed(ctx, "danger", "Ошибка",
                                    "Укажи положительную сумму или `all`.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    moved = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), src: {"$gte": amount}},
                        {"$inc": {"cash": -amount if to_bank else amount,
                                  "bank": amount if to_bank else -amount},
                         "$set": {"updated_at": now()}}, session=session)
                    if moved.modified_count != 1:
                        raise EconomyError("Недостаточно средств на счёте.")
                    await self.bot.journal(
                        f"user_{uid}_{src}", f"user_{uid}_{'bank' if to_bank else 'cash'}", amount,
                        "Пополнение банка" if to_bank else "Снятие наличных",
                        {"type": "deposit" if to_bank else "withdraw"},
                        session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        title = "💵 Снято" if not to_bank else "🏦 В банк"
        await send_embed(ctx, "success", title,
                         f"Переведено: **{fmt(amount)}** монет.")

    @commands.hybrid_command(name="deposit", aliases=["dep"], description="Положить в банк")
    @app_commands.describe(amount="Сумма или all")
    async def deposit(self, ctx, amount: str):
        await self._move_money(ctx, amount, to_bank=True)

    @commands.hybrid_command(name="withdraw", aliases=["with"], description="Снять наличные")
    @app_commands.describe(amount="Сумма или all")
    async def withdraw(self, ctx, amount: str):
        await self._move_money(ctx, amount, to_bank=False)

    @commands.hybrid_command(name="top", aliases=["t"], description="Топ-10 игроков")
    async def top(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        rows = await self.bot.db_users.aggregate([
            {"$match": {"guild_id": ctx.guild.id}},
            {"$addFields": {"net": {"$subtract": [
                {"$add": [{"$ifNull": ["$cash", 0]}, {"$ifNull": ["$bank", 0]}]},
                {"$ifNull": ["$debt", 0]}]}}},
            {"$sort": {"net": -1}}, {"$limit": 10},
        ]).to_list(10)
        if not rows:
            return await send_embed(ctx, "info", "Топ-10", "Пока нет игроков.")
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows, 1):
            uid = int(row["user_id"])
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"ID {uid}"
            icon = medals[i - 1] if i <= 3 else f"**{i}.**"
            lines.append(f"{icon} {name} — `{fmt(row['net'])}`")
        await send_embed(ctx, "info", "🏆 Топ-10 богатейших",
            description="\n".join(lines),
            thumbnail=ctx.guild.icon.url if ctx.guild.icon else None)

    # ---------- БИЗНЕС ----------
    @staticmethod
    def _business_text(doc, tax_rate):
        level = int(doc.get("level", 1))
        state = "🔒 заморожен" if doc.get("frozen") else "✅ работает"
        fields = [
            ("Уровень", f"`{level}` / {BUSINESS_MAX_LEVEL}", True),
            ("Статус", state, True),
            ("💵 Доход", f"`{fmt(business_income(level))}` / час", True),
            ("🧾 Налог", f"`{fmt(business_tax(level, tax_rate))}` / час ({fmt_pct(tax_rate)})", True),
        ]
        if level < BUSINESS_MAX_LEVEL:
            fields.append(("⬆️ Улучшение",
                f"`{fmt(upgrade_cost(level))}` с банковского счёта", False))
        else:
            fields.append(("⬆️ Улучшение", "Достигнут максимум", False))
        return fields

    @commands.hybrid_group(name="business", aliases=["biz"], description="Бизнес", invoke_without_command=True)
    async def business(self, ctx):
        if not await self._require_module(ctx, "business", "Бизнес"): return
        await self._show_business(ctx)

    @business.command(name="buy", description="Купить бизнес")
    async def business_buy(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx, "business", "Бизнес"): return
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        if int(user.get("debt", 0)) > 0:
            return await send_embed(ctx, "danger", "Ошибка",
                                    "Сначала погаси долг, затем покупай бизнес.")
        if await self.bot.businesses.find_one(ukey(gid, uid)):
            return await send_embed(ctx, "danger", "Ошибка", "У тебя уже есть бизнес.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    result = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "cash": {"$gte": BUSINESS_COST}, "debt": 0},
                        {"$inc": {"cash": -BUSINESS_COST}, "$set": {"updated_at": now()}},
                        session=session)
                    if result.modified_count != 1:
                        raise EconomyError(f"Нужно {fmt(BUSINESS_COST)} наличными и отсутствие долга.")
                    try:
                        await self.bot.businesses.insert_one({
                            **ukey(gid, uid), "level": 1, "last_payout": now(),
                            "frozen": False, "created_at": now()}, session=session)
                    except DuplicateKeyError:
                        raise EconomyError("У тебя уже есть бизнес.")
                    await self.bot.journal(f"user_{uid}", "business_market", BUSINESS_COST,
                                           "Покупка бизнеса", session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        await send_embed(ctx, "success", "🏢 Бизнес куплен",
            description=f"За **{fmt(BUSINESS_COST)}**.\nПервый доход — примерно через час.")

    @business.command(name="status", description="Статус бизнеса")
    async def business_status(self, ctx):
        if not await self._require_module(ctx, "business", "Бизнес"): return
        await self._show_business(ctx)

    async def _show_business(self, ctx):
        doc = await self.bot.businesses.find_one(ukey(ctx.guild.id, ctx.author.id))
        if not doc:
            return await send_embed(ctx, "info", "Нет бизнеса",
                                    "Используй `!business buy`.")
        cfg = await self.bot.get_settings(ctx.guild.id)
        await send_embed(ctx, "info", "🏢 Бизнес",
            fields=self._business_text(doc, cfg["business_tax"]),
            thumbnail=ctx.author.display_avatar.url)

    @business.command(name="upgrade", description="Улучшить бизнес")
    async def business_upgrade(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx, "business", "Бизнес"): return
        gid, uid = ctx.guild.id, ctx.author.id
        await self.bot.ensure_user(gid, uid)
        cfg = await self.bot.get_settings(gid)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    doc = await self.bot.businesses.find_one(ukey(gid, uid), session=session)
                    if not doc:
                        raise EconomyError("У тебя нет бизнеса. `!business buy`.")
                    level = int(doc.get("level", 1))
                    if level >= BUSINESS_MAX_LEVEL:
                        raise EconomyError("Бизнес уже на максимуме.")
                    cost = upgrade_cost(level)
                    if not await self.bot.change_bank(gid, uid, -cost, session=session):
                        raise EconomyError(f"Нужно `{fmt(cost)}` на банковском счёте.")
                    upgraded = await self.bot.businesses.update_one(
                        {**ukey(gid, uid), "level": level},
                        {"$inc": {"level": 1}, "$set": {"upgraded_at": now()}}, session=session)
                    if upgraded.modified_count != 1:
                        raise EconomyError("Бизнес изменился во время операции.")
                    await self.bot.change_reserve(gid, cost, session=session)
                    await self.bot.journal(f"user_{uid}", "central_bank", cost,
                                           "Улучшение бизнеса",
                                           {"from_level": level, "to_level": level + 1},
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        new_level = level + 1
        await send_embed(ctx, "success", "⬆️ Бизнес улучшен",
            fields=[
                ("Уровень", f"`{new_level}`", True),
                ("💵 Доход", f"`{fmt(business_income(new_level))}` / час", True),
                ("🧾 Налог", f"`{fmt(business_tax(new_level, cfg['business_tax']))}` / час", True),
            ])

    # ---------- КРЕДИТЫ ----------
    @commands.hybrid_group(name="loan", aliases=["ln"], description="Кредиты", invoke_without_command=True)
    async def loan(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        gid = ctx.guild.id
        data = await self.bot.get_balance(gid, ctx.author.id)
        cfg = await self.bot.get_settings(gid)
        fields = [("💳 Текущий долг", f"`{fmt(data['debt'])}`", False)]
        due = as_utc(data.get("loan_due"))
        if int(data.get("debt", 0)) > 0 and due:
            if now() > due:
                fields.append(("🚨 Статус", "Просрочен", False))
                if cfg["loan_penalty"] > 0:
                    fields.append(("📈 Штраф",
                        f"+{fmt_pct(cfg['loan_penalty'])} к долгу за сутки", False))
            else:
                fields.append(("📅 Погасить до",
                    f"<t:{int(due.timestamp())}:f> (<t:{int(due.timestamp())}:R>)", False))
        await send_embed(ctx, "info", "💳 Кредит", fields=fields,
            description="`!loan take сумма` — взять, `!loan pay сумма` — погасить.")

    @loan.command(name="take", description="Взять кредит")
    @app_commands.describe(amount="Сумма")
    async def loan_take(self, ctx, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if amount < LOAN_MIN or amount > LOAN_MAX:
            return await send_embed(ctx, "danger", "Ошибка",
                f"Сумма должна быть от {fmt(LOAN_MIN)} до {fmt(LOAN_MAX)}.")
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        if int(user.get("debt", 0)) > 0:
            return await send_embed(ctx, "danger", "Ошибка",
                                    "Новый кредит недоступен, пока не погашен текущий долг.")
        cfg = await self.bot.get_settings(gid)
        rate = float(cfg["loan_interest"])
        infl_mult = float(cfg.get("inflation_multiplier", 0.0))
        if infl_mult > 0:
            # процент растёт на inflation * 0.01 (с учётом множителя пресета)
            rate += await self.bot.current_inflation(gid) * 0.01 * infl_mult
        owed = int(amount * (1 + rate))
        due_at = now() + timedelta(seconds=LOAN_TERM)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.change_reserve(gid, -amount, session=session):
                        raise EconomyError("В резерве ЦБ недостаточно средств.")
                    taken = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "debt": 0},
                        {"$inc": {"cash": amount, "debt": owed},
                         "$set": {"updated_at": now(), "loan_penalty_at": None,
                                  "loan_due": due_at}}, session=session)
                    if taken.modified_count != 1:
                        raise EconomyError("Долг уже есть.")
                    await self.bot.journal("central_bank", f"user_{uid}", amount,
                                           "Выдача кредита", {"repayment_due": owed},
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        fields = [
            ("💰 Получено", f"`{fmt(amount)}`", True),
            ("💳 К возврату", f"`{fmt(owed)}` ({fmt_pct(rate)})", True),
            ("📅 Срок", f"<t:{int(due_at.timestamp())}:f>", False),
        ]
        if cfg["loan_penalty"] > 0:
            fields.append(("⚠️ Просрочка",
                f"+{fmt_pct(cfg['loan_penalty'])} к долгу за сутки", False))
        await send_embed(ctx, "success", "💳 Кредит выдан", fields=fields)

    @loan.command(name="pay", description="Погасить кредит")
    @app_commands.describe(amount="Сумма")
    async def loan_pay(self, ctx, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if amount <= 0:
            return await send_embed(ctx, "danger", "Ошибка", "Сумма должна быть положительной.")
        gid, uid = ctx.guild.id, ctx.author.id
        user = await self.bot.ensure_user(gid, uid)
        debt = int(user.get("debt", 0))
        if debt <= 0:
            return await send_embed(ctx, "info", "Нет долга", "У тебя нет активного кредита.")
        payment = min(amount, debt)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    result = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "cash": {"$gte": payment}, "debt": {"$gte": payment}},
                        {"$inc": {"cash": -payment, "debt": -payment},
                         "$set": {"updated_at": now()}}, session=session)
                    if result.modified_count != 1:
                        raise EconomyError("Недостаточно наличных.")
                    await self.bot.change_reserve(gid, payment, session=session)
                    if payment >= debt:
                        await self.bot.db_users.update_one(
                            {**ukey(gid, uid), "debt": 0},
                            {"$set": {"loan_due": None, "loan_penalty_at": None}},
                            session=session)
                    await self.bot.journal(f"user_{uid}", "central_bank", payment,
                                           "Погашение кредита", session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        await send_embed(ctx, "success", "✅ Кредит погашен",
            fields=[
                ("💸 Оплачено", f"`{fmt(payment)}`", True),
                ("💳 Остаток долга", f"`{fmt(debt - payment)}`", True),
            ])

    # ---------- ЦБ и экономика СЕРВЕРА ----------
    @commands.hybrid_command(name="cb", description="Центральный банк сервера")
    async def cb(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        s = await self.bot.stats(ctx.guild.id)
        extra = await self._inflation_fields(ctx.guild.id, s)
        await send_embed(ctx, "info", f"🏦 ЦБ — {ctx.guild.name}",
            fields=[
                ("💎 Резерв", f"`{fmt(s['reserve'])}`", True),
                ("💰 Госбюджет", f"`{fmt(s['budget'])}`", True),
                ("👥 Средства игроков", f"`{fmt(s['players'])}`", True),
                ("🪙 В обращении", f"`{fmt(s['circulation'])}`", True),
                ("📉 Дефицит ЦБ", f"`{fmt(s['debt'])}`", True),
                ("🖨️ Напечатано", f"`{fmt(s['printed'])}`", True),
            ] + extra,
            thumbnail=ctx.guild.icon.url if ctx.guild.icon else None,
            footer="Экономика изолирована — у каждого сервера свой ЦБ")

    @commands.hybrid_command(name="economy", description="Статистика сервера")
    async def economy(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        s = await self.bot.stats(ctx.guild.id)
        extra = await self._inflation_fields(ctx.guild.id, s)
        await send_embed(ctx, "info", f"📈 Экономика — {ctx.guild.name}",
            fields=[
                ("👥 Игроки", f"`{fmt(s['players'])}`", True),
                ("💳 Долги", f"`{fmt(s['player_debt'])}`", True),
                ("↗️ Приход 24ч", f"`{fmt(s['incoming'])}`", True),
                ("↘️ Расход 24ч", f"`{fmt(s['outgoing'])}`", True),
                ("🏦 Резерв ЦБ", f"`{fmt(s['reserve'])}`", True),
                ("💰 Госбюджет", f"`{fmt(s['budget'])}`", True),
            ] + extra,
            thumbnail=ctx.guild.icon.url if ctx.guild.icon else None)

    @commands.hybrid_command(name="print-money", with_app_command=False, description="Эмиссия в резерв ЦБ сервера")
    @commands.has_permissions(administrator=True)
    async def print_money(self, ctx, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if amount <= 0:
            return await send_embed(ctx, "danger", "Ошибка", "Сумма должна быть положительной.")
        await self.bot.ensure_guild_funds(ctx.guild.id)
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.bot.change_reserve(ctx.guild.id, amount, session=session)
                await self.bot.economy.update_one(
                    {"_id": self.bot.cb_id(ctx.guild.id)},
                    {"$inc": {"printed": amount}}, session=session)
                await self.bot.journal("money_printing", "central_bank", amount,
                                       "Эмиссия", session=session, guild_id=ctx.guild.id)
        self.bot.invalidate_inflation(ctx.guild.id)
        await send_embed(ctx, "success", "🖨️ Эмиссия проведена",
            fields=[
                ("💸 Напечатано", f"`{fmt(amount)}`", True),
                ("💎 Новый резерв", f"`{fmt(await self.bot.reserve(ctx.guild.id))}`", True),
            ],
            footer=f"Сервер: {ctx.guild.name}")

    @commands.hybrid_command(name="burn-money", with_app_command=False, description="Сжечь средства из резерва ЦБ сервера")
    @commands.has_permissions(administrator=True)
    async def burn_money(self, ctx, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if amount <= 0:
            return await send_embed(ctx, "danger", "Ошибка", "Сумма должна быть положительной.")
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    if not await self.bot.change_reserve(ctx.guild.id, -amount, session=session):
                        raise EconomyError("В резерве недостаточно средств.")
                    await self.bot.journal("central_bank", "money_burn", amount,
                                           "Сжигание денег", session=session, guild_id=ctx.guild.id)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        self.bot.invalidate_inflation(ctx.guild.id)
        await send_embed(ctx, "success", "🔥 Деньги сожжены",
            fields=[("💸 Сожжено", f"`{fmt(amount)}`", True),
                    ("💎 Новый резерв", f"`{fmt(await self.bot.reserve(ctx.guild.id))}`", True)])

    @commands.hybrid_command(name="set-report-channel", with_app_command=False, description="Канал ежедневных отчётов")
    @commands.has_permissions(administrator=True)
    async def setreportchannel(self, ctx, channel: discord.TextChannel = None):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        channel = channel or ctx.channel
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"report_channel_id": channel.id, "updated_at": now()}}, upsert=True)
        await send_embed(ctx, "success", "Канал отчётов установлен",
                         f"Ежедневные отчёты будут приходить в {channel.mention} в {REPORT_HOUR_UTC:02d}:00 UTC.")

    @commands.hybrid_command(name="set-chart-channel", with_app_command=False, description="Канал автоматических графиков")
    @commands.has_permissions(administrator=True)
    async def setchartchannel(self, ctx, channel: discord.TextChannel = None):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        channel = channel or ctx.channel
        await self.bot.settings.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"chart_channel_id": channel.id, "updated_at": now()}}, upsert=True)
        await send_embed(ctx, "success", "Канал графиков установлен",
                         f"Ежедневные графики будут приходить в {channel.mention}.")

    @commands.hybrid_command(name="graphs", aliases=["chart"], with_app_command=False, description="Графики экономики сервера")
    @commands.has_permissions(administrator=True)
    async def graphs(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        await ctx.defer()
        await self.bot.snapshot()
        delivered = await self.bot.publish_charts(ctx.guild.id, channel=ctx.channel)
        if delivered:
            return await send_embed(ctx, "success", "Графики отправлены",
                                    "Проверь этот канал.")
        await send_embed(ctx, "danger", "Не удалось отправить графики",
                         "Проверь права бота в этом канале.")


# =============================================================================
# КАЗИНО
# =============================================================================
def draw_card():
    return rng.randint(2, 11)

def hand_value(cards):
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def fmt_cards(cards, hide_second=False):
    if hide_second:
        return f"`{cards[0]} ?`"
    return " ".join(f"`{c}`" for c in cards) + f" = {hand_value(cards)}"

PAYTABLES = {
    "slot": "🎰 **Выплаты слотов 3×3**\nСовпавшие символы на линии (ряд, столбец или диагональ):\n• 1 линия — ×3\n• 2 линии — ×4\n• 3+ линий — ×10\nИначе — 0\nТеоретический возврат (RTP) ≈ 90%",
    "rr": "🔫 **Русская рулетка**\nШанс пули — 1 из 6 на каждый выстрел. Пусто — множитель растёт (×1.18, ×1.41, ×1.69…), пуля — ставка потеряна. «Забрать» — получить ставку × текущий множитель.",
    "blackjack": "🃏 **Блэкджек**\nНабери 21. Дилер берёт до 17.\nПобеда — ×2, ничья — возврат.",
}

async def reply_ephemeral(interaction: discord.Interaction, text: str):
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
    def __init__(self, game: str):
        super().__init__(label="Выплаты", style=discord.ButtonStyle.secondary, emoji="📋")
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        await reply_ephemeral(interaction, PAYTABLES.get(self.game, "—"))


class CasinoView(discord.ui.View):
    def __init__(self, cog, guild_id: int, user_id: int, bet: int, *, timeout: float):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.key = (guild_id, user_id)
        self.bet = bet
        self.lock = asyncio.Lock()
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await reply_ephemeral(interaction, "❌ Это не твоя игра.")
            return False
        return True

    async def has_funds(self, interaction: discord.Interaction) -> bool:
        user = await self.cog.bot.ensure_user(self.guild_id, self.user_id)
        if int(user.get("cash", 0)) < self.bet:
            await reply_ephemeral(interaction, "❌ Недостаточно средств.")
            return False
        return True

    async def _show(self, interaction, embed=None):
        kwargs = {"view": self}
        if embed is not None:
            kwargs["embed"] = embed
        try:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(**kwargs)
            elif self.message is not None:
                await self.message.edit(**kwargs)
        except (discord.Forbidden, discord.NotFound):
            log.warning("Не удалось обновить сообщение игры")
        except discord.HTTPException:
            log.exception("Ошибка обновления сообщения игры")

    async def on_play_again(self, interaction: discord.Interaction):
        raise NotImplementedError


class InstantGameView(CasinoView):
    def __init__(self, cog, guild_id: int, user_id: int, bet: int, game: str):
        super().__init__(cog, guild_id, user_id, bet, timeout=PLAY_AGAIN_TIMEOUT)
        self.game = game
        self.add_item(PlayAgainButton())
        self.add_item(PayoutsButton(game))

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
                        await self.message.edit(content=None, embed=make_embed("danger", "Ошибка", str(e)), view=self)
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
        self.stake_id = None            # запись в active_games для текущего раунда

    async def on_timeout(self):
        """Игрок пропал посреди раунда: авто-«Хватит», иначе ключ в active_bj залипнет навсегда."""
        async with self.lock:
            if self.done:
                for child in self.children:
                    child.disabled = True
                await self._show(None)
                return
            await self._finish(None, "stand", offer_again=False)

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
            if getattr(child, "custom_id", None) not in ("again",):
                if child is not self.children[-1] or self.done:
                    child.disabled = disabled

    def new_round(self):
        self.player = [draw_card(), draw_card()]
        self.dealer = [draw_card(), draw_card()]
        self.done = False
        self._lock_game_buttons(False)
        for child in self.children:
            if isinstance(child, PlayAgainButton):
                self.remove_item(child)

    async def _finish(self, interaction, outcome, *, offer_again=True):
        self.done = True
        if outcome == "bust":
            payout, text, color = 0, "💥 Перебор! Ставка проиграна.", discord.Color.red()
        elif outcome == "twentyone":
            payout, text, color = self.bet * 2, "🎉 Ровно 21! Выплата ×2.", discord.Color.green()
        else:
            while hand_value(self.dealer) < 17:
                self.dealer.append(draw_card())
            p, d = hand_value(self.player), hand_value(self.dealer)
            if d > 21 or p > d:
                payout, text, color = self.bet * 2, "✅ Ты победил! Выплата ×2.", discord.Color.green()
            elif p == d:
                payout, text, color = self.bet, "🤝 Ничья — ставка возвращена.", discord.Color.light_grey()
            else:
                payout, text, color = 0, "❌ Дилер победил. Ставка проиграна.", discord.Color.red()
        if payout <= 0:
            await self.cog.close_stakes([self.stake_id])
            self.stake_id = None
        if payout > 0:
            try:
                await self.cog.settle_payout(self.guild_id, self.user_id, payout, "blackjack",
                                             stake_id=self.stake_id)
                self.stake_id = None
                text += f"\nВыплата: **{fmt(payout)}**."
            except Exception:
                log.warning("Не удалось выплатить blackjack guild=%s uid=%s payout=%s",
                            self.guild_id, self.user_id, payout, exc_info=True)
                text += "\n⚠️ Ошибка выплаты — обратись к администратору."
        self.cog.active_bj.discard(self.key)
        self._lock_game_buttons(True)
        if offer_again:
            again_btn = PlayAgainButton()
            again_btn.custom_id = "again"
            self.add_item(again_btn)
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
                return await reply_ephemeral(interaction, "❌ У тебя уже идёт другая партия.")
            self.cog.active_bj.add(self.key)
            try:
                await self.cog.precheck(self.guild_id, self.user_id, self.bet)
                async with await self.cog.bot.mongo.start_session() as session:
                    async with session.start_transaction():
                        self.stake_id = await self.cog.take_bet(
                            self.guild_id, self.user_id, self.bet, "blackjack",
                            max_payout=self.bet * 2, session=session, track=True)
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
                await self._finish(None, "stand", offer_again=False)
            else:
                for child in self.children:
                    child.disabled = True
                await self._show(None)


# =============================================================================
# РУССКАЯ РУЛЕТКА (соло / мультиплеер + «Забрать»)
# =============================================================================
RR_GAME = "russian_roulette"

def rr_multiplier(steps: int) -> float:
    """0 выстрелов -> x1.00 (возврат ставки); дальше 0.98 * 1.2^n: 1.18, 1.41, 1.69, 2.03 …"""
    return 1.0 if steps <= 0 else round(RR_EDGE * RR_STEP_GROWTH ** steps, 2)


class RRPlayer:
    __slots__ = ("uid", "name", "bet", "steps", "status", "paid", "stake_id")

    def __init__(self, uid: int, name: str, bet: int, stake_id=None):
        self.uid, self.name, self.bet = int(uid), name, int(bet)
        self.stake_id = stake_id
        self.steps = 0
        self.status = "active"      # active | dead | cashed | won
        self.paid = 0

    @property
    def payout(self) -> int:
        return int(self.bet * rr_multiplier(self.steps))


class RRModeView(discord.ui.View):
    """Выбор режима: «Играть одному» / «Играть с друзьями». Ставка списывается после выбора."""
    def __init__(self, cog, host, guild_id: int, bet: int):
        super().__init__(timeout=RR_MODE_TIMEOUT)
        self.cog, self.host, self.guild_id, self.bet = cog, host, int(guild_id), int(bet)
        self.message = None
        self.chosen = False
        # Ключ (guild, host) уже занят в active_rr командой !rr ещё до первого await.
        self.reserved = True

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await reply_ephemeral(interaction, "❌ Эта игра идёт на другом сервере.")
            return False
        if interaction.user.id != self.host.id:
            await reply_ephemeral(interaction, "❌ Режим выбирает тот, кто начал игру.")
            return False
        return True

    @discord.ui.button(label="Играть одному", style=discord.ButtonStyle.primary, emoji="🎯")
    async def btn_solo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, multi=False)

    @discord.ui.button(label="Играть с друзьями", style=discord.ButtonStyle.success, emoji="👥")
    async def btn_friends(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, multi=True)

    async def _fail(self, interaction: discord.Interaction, text: str):
        self.stop()
        try:
            await interaction.edit_original_response(
                embed=make_embed("danger", "Русская рулетка", text), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _choose(self, interaction: discord.Interaction, multi: bool):
        if self.chosen:
            return await interaction.response.defer()
        self.chosen = True
        await interaction.response.defer()
        gid, uid = self.guild_id, self.host.id
        key = (gid, uid)
        if not self.reserved:
            if key in self.cog.active_rr:
                return await self._fail(interaction, "У тебя уже идёт игра в рулетку.")
            self.cog.active_rr.add(key)
        try:
            stake_id = await self.cog.rr_take_stake(gid, uid, self.bet)
        except EconomyError as e:
            self.cog.active_rr.discard(key)
            return await self._fail(interaction, str(e))
        except Exception:
            self.cog.active_rr.discard(key)
            await self._fail(interaction, "Внутренняя ошибка. Попробуй ещё раз.")
            raise
        self.stop()
        player = RRPlayer(uid, self.host.display_name, self.bet, stake_id)
        if multi:
            await self.cog.rr_run_lobby(self.message, gid, player)
        else:
            await self.cog.rr_begin(self.message, gid, [player])

    async def on_timeout(self):
        if self.chosen:
            return
        if self.reserved:                      # режим не выбран, ставка не списывалась
            self.cog.active_rr.discard((self.guild_id, self.host.id))
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class RRLobbyView(discord.ui.View):
    """Лобби: кнопка «Присоединиться» (ставка списывается сразу). Закрывается через RR_LOBBY_SECONDS."""
    def __init__(self, cog, guild_id: int, host_player: RRPlayer, deadline: int):
        super().__init__(timeout=None)          # время контролирует asyncio.sleep в rr_run_lobby
        self.cog, self.guild_id, self.bet, self.deadline = cog, int(guild_id), host_player.bet, deadline
        self.players = [host_player]
        self.lock = asyncio.Lock()
        self.closed = False

    def render(self):
        lines = "\n".join(f"• <@{p.uid}>" for p in self.players)
        return make_embed("info", "🔫 Русская рулетка — Лобби",
            description=(f"Ставка: {COIN_EMOJI} `{fmt(self.bet)}` с каждого игрока.\n"
                         f"Нажми «Присоединиться», чтобы сыграть вместе.\n"
                         f"Старт <t:{self.deadline}:R>. Если никто не зайдёт — игра пойдёт в соло."),
            fields=[(f"👥 Игроки ({len(self.players)}/{RR_MAX_PLAYERS})", lines, False)])

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await reply_ephemeral(interaction, "❌ Это лобби другого сервера.")
            return False
        return True

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.success, emoji="➕")
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        user = interaction.user
        key = (self.guild_id, user.id)
        async with self.lock:
            if self.closed:
                return await reply_ephemeral(interaction, "❌ Лобби уже закрыто.")
            if any(p.uid == user.id for p in self.players):
                return await reply_ephemeral(interaction, "❌ Ты уже в лобби.")
            if len(self.players) >= RR_MAX_PLAYERS:
                return await reply_ephemeral(interaction, "❌ Лобби заполнено.")
            if key in self.cog.active_rr:
                return await reply_ephemeral(interaction, "❌ У тебя уже идёт игра в рулетку.")
            self.cog.active_rr.add(key)
            try:
                stake_id = await self.cog.rr_take_stake(self.guild_id, user.id, self.bet)
            except EconomyError as e:
                self.cog.active_rr.discard(key)
                return await reply_ephemeral(interaction, f"❌ {e}")
            except Exception:
                self.cog.active_rr.discard(key)
                raise
            self.players.append(RRPlayer(user.id, user.display_name, self.bet, stake_id))
            embed = self.render()
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Не удалось обновить лобби рулетки")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        log.error("Ошибка лобби рулетки", exc_info=error)
        await reply_ephemeral(interaction, "⚠️ Внутренняя ошибка. Попробуй ещё раз.")


class RRGameView(discord.ui.View):
    """Игровой цикл. Ставки уже списаны (cash -> резерв ЦБ); выплаты — settle_payout (транзакция).

    Банк проигравших (pot) = ставки выбывших. Последний оставшийся игрок в мультиплеере
    автоматически побеждает и получает свою выплату + банк проигравших."""
    def __init__(self, cog, guild_id: int, message, players):
        super().__init__(timeout=None)          # ходы контролирует watchdog (RR_TURN_TIMEOUT)
        self.cog, self.guild_id, self.message = cog, int(guild_id), message
        self.players = list(players)
        self.multi = len(self.players) > 1
        self.turn = 0
        self.pot = 0
        self.log = []
        self.lock = asyncio.Lock()
        self.ended = False
        self._timer = None
        self._turn_id = 0
        self._sync_buttons()

    @property
    def current(self) -> RRPlayer:
        return self.players[self.turn]

    # ---------- отображение ----------
    def _sync_buttons(self):
        self.btn_shoot.disabled = self.ended
        self.btn_cash.disabled = self.ended or self.current.steps <= 0     # забрать можно после 1-го выстрела

    def render(self):
        cur = self.current
        title = "🔫 Русская рулетка — " + ("Мультиплеер" if self.multi else "Соло")
        tail = "\n".join(self.log[-5:])
        if self.ended:
            won = any(p.status in ("won", "cashed") and p.paid > p.bet for p in self.players)
            color = discord.Color.green() if won else discord.Color.red()
            embed = discord.Embed(title=title, description=(tail + "\n\n" if tail else "") + "**Игра окончена.**",
                                  color=color, timestamp=now())
            embed.add_field(name="Итог", value=self._final_text(), inline=False)
        else:
            nxt = cur.steps + 1
            next_txt = f"x{rr_multiplier(nxt):.2f}" if nxt <= RR_MAX_STEPS else "—"
            head = f"Ходит: <@{cur.uid}>\n\n" if self.multi else ""
            embed = discord.Embed(title=title, description=head + tail, color=discord.Color.gold(),
                                  timestamp=now())
            embed.add_field(name="💰 Текущий банк (выплата)",
                            value=f"{COIN_EMOJI} `{fmt(cur.payout)}` (x{rr_multiplier(cur.steps):.2f})", inline=True)
            embed.add_field(name="⏭️ Следующий множитель", value=f"`{next_txt}`", inline=True)
            embed.add_field(name="🎯 Пережито выстрелов", value=f"`{cur.steps}`", inline=True)
        if self.multi:
            embed.add_field(name="🏦 Банк проигравших", value=f"{COIN_EMOJI} `{fmt(self.pot)}`", inline=True)
            lines = []
            for p in self.players:
                if p.status == "dead":
                    lines.append(f"💀 <@{p.uid}> — выбыл")
                elif p.status == "cashed":
                    lines.append(f"💰 <@{p.uid}> — забрал `{fmt(p.paid)}`")
                elif p.status == "won":
                    lines.append(f"🏆 <@{p.uid}> — победитель `{fmt(p.paid)}`")
                else:
                    mark = "👉" if (not self.ended and p is self.current) else "🟢"
                    lines.append(f"{mark} <@{p.uid}> — x{rr_multiplier(p.steps):.2f} (`{fmt(p.payout)}`)")
            embed.add_field(name="👥 Игроки", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"Ставка: {fmt(self.players[0].bet)} • шанс пули 1/{RR_BULLET_SIDES}"
                              + ("" if self.ended else f" • {RR_TURN_TIMEOUT} с на ход"))
        return embed

    def _final_text(self) -> str:
        if not self.multi:
            p = self.players[0]
            if p.status == "cashed":
                return f"💰 Выплата: {COIN_EMOJI} `{fmt(p.paid)}` (x{rr_multiplier(p.steps):.2f})"
            return f"💥 Ставка потеряна: {COIN_EMOJI} `{fmt(p.bet)}`"
        winner = next((p for p in self.players if p.status == "won"), None)
        if winner:
            return f"🏆 Победитель: <@{winner.uid}> — забрал {COIN_EMOJI} `{fmt(winner.paid)}` (с банком проигравших)"
        return "Игра завершена без победителя."

    async def _push(self, interaction=None):
        self._sync_buttons()
        embed = self.render()
        try:
            if interaction is not None:
                await interaction.edit_original_response(embed=embed, view=self)
                return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        try:
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden):
            log.warning("Не удалось обновить сообщение рулетки")
        except discord.HTTPException:
            log.exception("Ошибка обновления сообщения рулетки")

    # ---------- таймер хода ----------
    def _cancel_timer(self):
        t = self._timer
        if t is not None and t is not asyncio.current_task():
            t.cancel()
        self._timer = None

    def arm_timer(self):
        self._cancel_timer()
        self._turn_id += 1
        self._timer = asyncio.create_task(self._watchdog(self._turn_id))

    async def _watchdog(self, tid: int):
        try:
            await asyncio.sleep(RR_TURN_TIMEOUT)
            async with self.lock:
                if self.ended or tid != self._turn_id:
                    return
                await self._auto_resolve()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка таймера рулетки")
            await self.abort("Внутренняя ошибка.")

    async def _auto_resolve(self):
        """Игрок не ответил: есть выстрелы — авто-«Забрать»; иначе соло — возврат ставки, мульти — выбывание."""
        p = self.current
        if p.steps > 0:
            self.log.append(f"⏰ **{p.name}** не ответил — деньги забираются автоматически.")
            await self._cash_out(p)
        elif self.multi:
            p.status = "dead"
            self.pot += p.bet
            self.log.append(f"⏰ **{p.name}** не ответил — выбывает, ставка уходит в банк проигравших.")
        else:
            await self.cog.settle_payout(self.guild_id, p.uid, p.bet, "russian_roulette_refund",
                                         stake_id=p.stake_id)
            p.status, p.paid = "cashed", p.bet
            self.log.append(f"⏰ **{p.name}** не стрелял — ставка возвращена.")
        await self._after_action(None)

    # ---------- проверки ----------
    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await reply_ephemeral(interaction, "❌ Эта игра идёт на другом сервере.")
            return False
        if not any(p.uid == interaction.user.id for p in self.players):
            await reply_ephemeral(interaction, "❌ Ты не участвуешь в этой игре.")
            return False
        if self.ended:
            await reply_ephemeral(interaction, "❌ Игра уже окончена.")
            return False
        if interaction.user.id != self.current.uid:
            await reply_ephemeral(interaction, "❌ Сейчас ход другого игрока.")
            return False
        return True

    async def _still_turn(self, interaction: discord.Interaction) -> bool:
        if self.ended or interaction.user.id != self.current.uid:
            await reply_ephemeral(interaction, "❌ Ход уже перешёл к другому игроку.")
            return False
        return True

    # ---------- действия ----------
    async def _cash_out(self, p: RRPlayer):
        payout = p.payout
        await self.cog.settle_payout(self.guild_id, p.uid, payout, RR_GAME, stake_id=p.stake_id)  # транзакция
        p.status, p.paid = "cashed", payout
        self.log.append(f"💰 **{p.name}** забирает {COIN_EMOJI} `{fmt(payout)}` (x{rr_multiplier(p.steps):.2f}).")

    async def _after_action(self, interaction):
        if await self._maybe_finish(interaction):
            return
        self.turn = self._next_turn()
        self.arm_timer()
        await self._push(interaction)

    def _next_turn(self) -> int:
        n = len(self.players)
        for i in range(1, n + 1):
            idx = (self.turn + i) % n
            if self.players[idx].status == "active":
                return idx
        return self.turn

    async def _maybe_finish(self, interaction) -> bool:
        active = [p for p in self.players if p.status == "active"]
        if (self.multi and len(active) > 1) or (not self.multi and active):
            return False
        if self.multi and len(active) == 1:
            w = active[0]
            total = w.payout + self.pot
            await self.cog.settle_payout(self.guild_id, w.uid, total, RR_GAME, stake_id=w.stake_id)  # транзакция
            w.status, w.paid = "won", total
            self.log.append(f"🏆 **{w.name}** — последний выживший! Забирает {COIN_EMOJI} `{fmt(total)}`.")
        # Игра окончена: закрываем оставшиеся записи (проигравшие / банк уже распределён)
        await self.cog.close_stakes([p.stake_id for p in self.players])
        self.ended = True
        self._cancel_timer()
        self._release()
        for child in self.children:
            child.disabled = True
        self.stop()
        await self._push(interaction)
        return True

    def _release(self):
        for p in self.players:
            self.cog.active_rr.discard((self.guild_id, p.uid))

    @discord.ui.button(label="Выстрелить", style=discord.ButtonStyle.danger, emoji="🔫")
    async def btn_shoot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self.lock:
            if not await self._still_turn(interaction):
                return
            self._cancel_timer()
            p = self.current
            if rng.randint(1, RR_BULLET_SIDES) == 1:
                p.status = "dead"
                self.pot += p.bet
                self.log.append(f"💥 **{p.name}** выстрелил — *БАХ!* "
                                + ("Ставка потеряна." if not self.multi
                                   else "Выбывает, ставка уходит в банк проигравших."))
            else:
                p.steps += 1
                self.log.append(f"🔫 **{p.name}** выстрелил — *щёлк…* Пусто! Множитель x{rr_multiplier(p.steps):.2f}.")
                if p.steps >= RR_MAX_STEPS:
                    self.log.append(f"🎯 Достигнут максимум ({RR_MAX_STEPS} выстрелов) — выплата автоматическая.")
                    await self._cash_out(p)
            await self._after_action(interaction)

    @discord.ui.button(label="Забрать", style=discord.ButtonStyle.success, emoji="💰")
    async def btn_cash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self.lock:
            if not await self._still_turn(interaction):
                return
            p = self.current
            if p.steps <= 0:
                return await reply_ephemeral(interaction, "❌ Сначала нужно выстрелить хотя бы раз.")
            self._cancel_timer()
            await self._cash_out(p)
            await self._after_action(interaction)

    # ---------- аварийное завершение ----------
    async def abort(self, reason: str):
        async with self.lock:
            if self.ended:
                return
            self.ended = True
            self._cancel_timer()
            for p in self.players:
                if p.status == "active":        # активным игрокам возвращаем ставку
                    try:
                        await self.cog.settle_payout(self.guild_id, p.uid, p.bet, "russian_roulette_refund",
                                                     stake_id=p.stake_id)
                        p.status, p.paid = "cashed", p.bet
                    except Exception:
                        log.exception("Не удалось вернуть ставку при аварии рулетки uid=%s", p.uid)
            await self.cog.close_stakes([p.stake_id for p in self.players])
            self._release()
            for child in self.children:
                child.disabled = True
            self.stop()
            try:
                await self.message.edit(embed=make_embed(
                    "danger", "Русская рулетка прервана",
                    f"{reason}\nСтавки активных игроков возвращены."), view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        log.error("Ошибка в русской рулетке", exc_info=error)
        await self.abort("Внутренняя ошибка.")


class CasinoCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot
        self.active_bj = set()
        self.active_rr = set()      # (guild_id, user_id) игроков в активной рулетке

    async def _require_module(self, ctx, title: str) -> bool:
        if await self.bot.module_enabled(ctx.guild.id, "casino"):
            return True
        await send_embed(ctx, "warning", f"{title} отключён",
                         "Администратор сервера выключил этот модуль. "
                         "Включить можно через `/settings`.")
        return False

    async def _resolve_bet(self, ctx, raw):
        user = await self.bot.ensure_user(ctx.guild.id, ctx.author.id)
        cash = int(user.get("cash", 0))
        bet = parse_amount(raw, min(cash, CASINO_MAX_BET))
        if bet is None:
            await send_embed(ctx, "danger", "Ошибка", "Укажи ставку числом или `all`.")
        return bet

    async def precheck(self, gid: int, uid: int, bet: int):
        if bet < CASINO_MIN_BET or bet > CASINO_MAX_BET:
            raise EconomyError(f"Ставка должна быть от {fmt(CASINO_MIN_BET)} до {fmt(CASINO_MAX_BET)}.")
        user = await self.bot.ensure_user(gid, uid)
        cash = int(user.get("cash", 0))
        if cash < bet:
            raise EconomyError(f"Недостаточно наличных: у тебя `{fmt(cash)}`, ставка `{fmt(bet)}`.")

    async def take_bet(self, gid: int, uid: int, bet: int, game: str, *, max_payout: int, session,
                       track: bool = False):
        """Списывает ставку. track=True — пишет запись в active_games В ТОЙ ЖЕ транзакции
        (для многошаговых игр: bj, rr) и возвращает её id; slot завершается в одной транзакции."""
        debit = await self.bot.db_users.update_one(
            {**ukey(gid, uid), "cash": {"$gte": bet}},
            {"$inc": {"cash": -bet}, "$set": {"updated_at": now()}},
            session=session,
        )
        if debit.modified_count != 1:
            raise EconomyError("Недостаточно наличных.")
        await self.bot.change_reserve(gid, bet, session=session)
        if await self.bot.reserve(gid, session=session) < max_payout:
            raise EconomyError("Резерв ЦБ сервера слишком мал. Уменьши ставку или попроси админа сделать эмиссию.")
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
        if not track:
            return None
        stake_id = uuid.uuid4().hex
        await self.bot.active_games.insert_one(
            {"_id": stake_id, "guild_id": int(gid), "user_id": int(uid), "game": key,
             "bet": int(bet), "created_at": now()}, session=session)
        return stake_id

    async def pay_out(self, gid: int, uid: int, payout: int, game: str, *, session, force=False):
        if payout <= 0:
            return
        if not await self.bot.change_reserve(gid, -payout, session=session, allow_negative=force):
            raise EconomyError("Резерв ЦБ сервера не может покрыть выигрыш.")
        await self.bot.change_cash(gid, uid, payout, session=session)
        key = game.replace("_refund", "")
        await self.bot.casino_stats.update_one(
            ukey(gid, uid),
            {"$inc": {"paid": payout, f"by_game.{key}.paid": payout}, "$set": {"updated_at": now()}},
            upsert=True, session=session,
        )
        await self.bot.journal("central_bank", f"user_{uid}", payout, f"Казино: выплата ({game})",
                               {"game": game}, session=session, guild_id=gid)

    async def settle_payout(self, gid: int, uid: int, payout: int, game: str, *, stake_id=None):
        """Выплата + закрытие записи ставки (stake_id) в одной транзакции."""
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                if stake_id:
                    await self.bot.active_games.delete_one({"_id": stake_id}, session=session)
                await self.pay_out(gid, uid, payout, game, session=session, force=True)

    async def close_stakes(self, stake_ids):
        """Закрывает записи ставок без выплаты (проигрыш / игра окончена). Идемпотентно."""
        ids = [i for i in stake_ids if i]
        if not ids:
            return
        try:
            await self.bot.active_games.delete_many({"_id": {"$in": ids}})
        except Exception:
            log.exception("Не удалось закрыть записи ставок: %s", ids)

    async def recover_active_games(self):
        """При старте: любая запись в active_games = игра не завершена -> рефанд ставки и удаление записи.
        Удаление и возврат — одна транзакция; если запись уже удалена (другой инстанс) — пропускаем."""
        docs = await self.bot.active_games.find({}).to_list(None)
        refunded = 0
        for doc in docs:
            try:
                gid, uid, bet = int(doc["guild_id"]), int(doc["user_id"]), int(doc["bet"])
                game = str(doc.get("game", "game"))
                async with await self.bot.mongo.start_session() as session:
                    async with session.start_transaction():
                        deleted = await self.bot.active_games.delete_one({"_id": doc["_id"]}, session=session)
                        if deleted.deleted_count != 1:
                            continue
                        if bet > 0:
                            await self.pay_out(gid, uid, bet, f"{game}_refund", session=session, force=True)
                refunded += 1
                log.warning("Рефанд после рестарта: guild=%s uid=%s game=%s bet=%s", gid, uid, game, bet)
            except Exception:
                log.exception("Не удалось вернуть ставку после рестарта: %s", doc.get("_id"))
        if docs:
            log.warning("Незавершённых игр: %s, возвращено ставок: %s", len(docs), refunded)

    def _result_embed(self, title, body, bet, payout, cash, color):
        return make_embed(
            "success" if payout > 0 else ("danger" if payout == 0 and bet > 0 else "info"),
            title, description=body,
            fields=[("Выплата", f"{COIN_EMOJI} `{fmt(payout)}`", True),
                    ("Наличные", f"`{fmt(cash)}`", True)],
            footer=f"Ставка: {fmt(bet)}")

    @staticmethod
    def _slot_grid_text(grid):
        return "\n".join(" │ ".join(row) for row in grid)

    @staticmethod
    def _slot_winning_lines(grid):
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
        await self.precheck(gid, uid, bet)
        final_grid = [[rng.choice(SLOT_SYMBOLS) for _ in range(3)] for _ in range(3)]
        if progress_callback is not None:
            for frame in range(3):
                animated_grid = [[rng.choice(SLOT_SYMBOLS) for _ in range(3)] for _ in range(3)]
                anim_embed = make_embed("info", "🎰 Слоты крутятся…",
                    description=f"```\n{self._slot_grid_text(animated_grid)}\n```",
                    footer=f"Ставка: {fmt(bet)} • Кадр {frame + 1}/3")
                try:
                    await progress_callback(anim_embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    progress_callback = None
                    break
                await asyncio.sleep(1)
        winning_lines = self._slot_winning_lines(final_grid)
        line_count = len(winning_lines)
        multiplier = slot_multiplier(line_count)
        payout = bet * multiplier
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                await self.take_bet(gid, uid, bet, "slot", max_payout=bet * SLOT_MAX_MULT, session=session)
                await self.pay_out(gid, uid, payout, "slot", session=session)
        cash = int((await self.bot.get_balance(gid, uid)).get("cash", 0))
        title = (f"🎉 Джекпот! ×{multiplier}" if line_count >= 3 else
                 f"✨ Выигрыш ×{multiplier}" if line_count else
                 "🎰 Не повезло в этот раз")
        body = f"```\n{self._slot_grid_text(final_grid)}\n```"
        if line_count:
            body += f"\nСовпавших линий: **{line_count}** из 8."
        else:
            body += "\nСовпавших линий нет."
        return self._result_embed(title, body, bet, payout, cash,
                                  discord.Color.gold() if line_count >= 3 else
                                  (discord.Color.green() if line_count else discord.Color.red()))

    async def run_game(self, game: str, gid: int, uid: int, bet: int, progress_callback=None):
        if game == "slot":
            return await self.run_slot(gid, uid, bet, progress_callback=progress_callback)
        raise ValueError(game)

    async def _start_instant(self, ctx, game: str, bet: int):
        gid, uid = ctx.guild.id, ctx.author.id
        view = InstantGameView(self, gid, uid, bet, game)
        if game == "slot":
            for child in view.children:
                child.disabled = True
            placeholder = make_embed("info", "🎰 Слоты запускаются…",
                description="```\n❔ │ ❔ │ ❔\n❔ │ ❔ │ ❔\n❔ │ ❔ │ ❔\n```")
            view.message = await safe_send(ctx, embed=placeholder, view=view)
            if view.message is None:
                return
            async def progress(embed):
                await view.message.edit(embed=embed, view=view)
            try:
                embed = await self.run_game(game, gid, uid, bet, progress_callback=progress)
            except EconomyError as e:
                await view.message.edit(content=None, embed=make_embed("danger", "Ошибка", str(e)), view=None)
                return
            for child in view.children:
                child.disabled = False
            await view.message.edit(embed=embed, view=view)
            return
        try:
            embed = await self.run_game(game, gid, uid, bet)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Ошибка", str(e))
        view.message = await safe_send(ctx, embed=embed, view=view)

    GAME_LABELS = {"slot": "🎰 Слоты", "russian_roulette": "🔫 Рулетка", "blackjack": "🃏 Блэкджек"}

    @commands.hybrid_command(name="casino-stats", aliases=["cs", "cstats"], description="Статистика казино игрока")
    @app_commands.describe(member="Чью статистику показать")
    async def casinostats(self, ctx, member: discord.Member = None):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        member = member or ctx.author
        doc = await self.bot.casino_stats.find_one(ukey(ctx.guild.id, member.id))
        if not doc or not int(doc.get("games", 0)):
            return await send_embed(ctx, "info", "🎲 Казино",
                                    f"{member.display_name} ещё не играл в казино.",
                                    thumbnail=member.display_avatar.url)
        wagered, paid, games = int(doc.get("wagered", 0)), int(doc.get("paid", 0)), int(doc.get("games", 0))
        net = paid - wagered
        rtp = paid / wagered * 100 if wagered else 0
        fields = [
            ("Игр", f"`{fmt(games)}`", True),
            ("Поставлено", f"`{fmt(wagered)}`", True),
            ("Выплачено", f"`{fmt(paid)}`", True),
            ("Итог", f"`{'+' if net >= 0 else '-'}{fmt(abs(net))}` ({rtp:.0f}% возврата)", False),
        ]
        lines = []
        for key, row in (doc.get("by_game") or {}).items():
            g_net = int(row.get("paid", 0)) - int(row.get("wagered", 0))
            lines.append(f"{self.GAME_LABELS.get(key, key)}: {fmt(row.get('games', 0))} игр, "
                         f"{'+' if g_net >= 0 else '-'}{fmt(abs(g_net))}")
        if lines:
            fields.append(("По играм", "\n".join(lines), False))
        await send_embed(ctx, "success" if net >= 0 else "danger",
                         f"🎲 Казино — {member.display_name}", fields=fields,
                         thumbnail=member.display_avatar.url)

    @commands.hybrid_command(name="slot", aliases=["sl"], description="Слоты 3×3")
    @app_commands.describe(bet="Ставка наличными или all")
    async def slot(self, ctx, bet: str):
        if not await self._require_module(ctx, "🎰 Казино"): return
        bet = await self._resolve_bet(ctx, bet)
        if bet is None: return
        await self._start_instant(ctx, "slot", bet)

    # ---------- Русская рулетка: соло / с друзьями + «Забрать» ----------
    @commands.hybrid_command(name="rr", aliases=["roulette"],
                             description="Русская рулетка: соло или с друзьями, можно забрать выигрыш")
    @app_commands.describe(bet="Ставка наличными или all")
    async def rr(self, ctx, bet: str):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        gid, uid = ctx.guild.id, ctx.author.id
        key = (gid, uid)
        # Проверка и резервирование — подряд, БЕЗ await между ними. Ключ переходит во владение
        # RRModeView (reserved=True) и освобождается по окончании игры / таймауту выбора режима.
        if key in self.active_rr:
            return await send_embed(ctx, "danger", "Ошибка", "У тебя уже идёт игра в рулетку.")
        self.active_rr.add(key)
        handed_off = False
        try:
            if not await self._require_module(ctx, "🎰 Казино"): return
            bet = await self._resolve_bet(ctx, bet)
            if bet is None: return
            try:
                await self.precheck(gid, uid, bet)
            except EconomyError as e:
                return await send_embed(ctx, "danger", "Ошибка", str(e))
            view = RRModeView(self, ctx.author, gid, bet)
            embed = make_embed("info", "🔫 Русская рулетка",
                description=("Шанс пули — **1 из 6** на каждый выстрел.\n"
                             "Пусто — множитель растёт (×1.18, ×1.41, ×1.69 …), пуля — ставка потеряна.\n"
                             "«Забрать» — получить ставку × текущий множитель.\n\n"
                             "Выбери режим:"),
                fields=[("💰 Ставка", f"{COIN_EMOJI} `{fmt(bet)}`", True)],
                footer="Ставка спишется после выбора режима")
            msg = await safe_send(ctx, embed=embed, view=view)
            if msg is None:
                return
            handed_off = True
            try:
                view.message = ctx.channel.get_partial_message(msg.id)   # правки по токену бота, без лимита 15 минут
            except AttributeError:
                view.message = msg
        finally:
            if not handed_off:
                self.active_rr.discard(key)

    # ----- финансы рулетки (все операции — в транзакциях) -----
    async def rr_take_stake(self, gid: int, uid: int, bet: int):
        await self.precheck(gid, uid, bet)
        async with await self.bot.mongo.start_session() as session:
            async with session.start_transaction():
                return await self.take_bet(gid, uid, bet, RR_GAME, session=session, track=True,
                                           max_payout=int(bet * rr_multiplier(RR_MAX_STEPS)))

    async def rr_refund(self, gid: int, players):
        for p in players:
            try:
                await self.settle_payout(gid, p.uid, p.bet, "russian_roulette_refund", stake_id=p.stake_id)
            except Exception:
                log.exception("Не удалось вернуть ставку рулетки guild=%s uid=%s", gid, p.uid)
            self.active_rr.discard((gid, p.uid))

    async def rr_run_lobby(self, message, gid: int, host_player):
        """Лобби на RR_LOBBY_SECONDS секунд. Никто не зашёл — соло, зашли — мультиплеер."""
        deadline = int(time.time()) + RR_LOBBY_SECONDS
        lobby = RRLobbyView(self, gid, host_player, deadline)
        try:
            await message.edit(embed=lobby.render(), view=lobby)
            await asyncio.sleep(RR_LOBBY_SECONDS)
            async with lobby.lock:          # дожидаемся уже идущих присоединений
                lobby.closed = True
            lobby.stop()
        except Exception:
            lobby.closed = True
            lobby.stop()
            log.exception("Ошибка лобби рулетки guild=%s", gid)
            await self.rr_refund(gid, lobby.players)
            return
        await self.rr_begin(message, gid, list(lobby.players))

    async def rr_begin(self, message, gid: int, players):
        game = RRGameView(self, gid, message, players)
        try:
            await message.edit(embed=game.render(), view=game)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Не удалось показать игру в рулетку guild=%s", gid)
            await game.abort("Не удалось отправить сообщение с игрой.")
            return
        game.arm_timer()

    @commands.hybrid_command(name="bj", aliases=["blackjack"], description="Блэкджек")
    @app_commands.describe(bet="Ставка наличными или all")
    async def bj(self, ctx, bet: str):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        gid, uid = ctx.guild.id, ctx.author.id
        key = (gid, uid)
        # Проверка и резервирование — подряд, БЕЗ await между ними: два быстрых вызова не пройдут оба.
        if key in self.active_bj:
            return await send_embed(ctx, "danger", "Ошибка", "У тебя уже идёт партия в блэкджек.")
        self.active_bj.add(key)
        handed_off = False      # True — дальше ключ освобождает сама партия (BlackjackView._finish)
        try:
            if not await self._require_module(ctx, "🎰 Казино"): return
            bet = await self._resolve_bet(ctx, bet)
            if bet is None: return
            try:
                await self.precheck(gid, uid, bet)
                async with await self.bot.mongo.start_session() as session:
                    async with session.start_transaction():
                        stake_id = await self.take_bet(gid, uid, bet, "blackjack",
                                                       max_payout=bet * 2, session=session, track=True)
            except EconomyError as e:
                return await send_embed(ctx, "danger", "Ошибка", str(e))
            view = BlackjackView(self, gid, uid, bet)
            view.stake_id = stake_id
            message = await safe_send(ctx, embed=view.render(), view=view)
            if message is None:
                try:
                    await self.settle_payout(gid, uid, bet, "blackjack_refund", stake_id=stake_id)
                except Exception:
                    log.exception("Не удалось вернуть ставку blackjack")
                return
            view.message = message
            handed_off = True
            if hand_value(view.player) == 21:
                async with view.lock:
                    await view._finish(None, "twentyone")
        finally:
            if not handed_off:
                self.active_bj.discard(key)


# =============================================================================
# БИРЖА: вспомогательные функции и кнопки
# =============================================================================
def stock_sid(guild_id, ticker) -> str:
    return f"{int(guild_id)}:{ticker}"

def hkey(guild_id, user_id, ticker) -> dict:
    return {"guild_id": int(guild_id), "user_id": int(user_id), "ticker": ticker}

def fmt_price(value) -> str:
    return f"{float(value):,.2f}".replace(",", " ")

def stock_rows(st) -> list:
    """История цены акции -> [{'created_at': datetime(UTC), 'price': float}, ...]."""
    prices = st.get("history") or []
    stamps = st.get("history_ts") or []
    rows = []
    for p, ts in zip(prices, stamps):
        ts = as_utc(ts)
        if ts is not None:
            rows.append({"created_at": ts, "price": float(p)})
    cur = float(st.get("price", 0) or 0)
    # сделки меняют цену без записи в историю — добавляем текущую точку на график
    if cur > 0 and (not rows or abs(rows[-1]["price"] - cur) > 1e-9):
        rows.append({"created_at": as_utc(st.get("updated_at")) or now(), "price": cur})
    if not rows:
        rows.append({"created_at": as_utc(st.get("created_at")) or now(), "price": cur or STOCK_START_PRICE})
    return rows

def stock_change_24h(rows, price) -> float:
    """Изменение цены за 24 часа в процентах."""
    if not rows:
        return 0.0
    cutoff = now() - timedelta(hours=24)
    base = rows[0]["price"]
    for r in rows:
        if r["created_at"] <= cutoff:
            base = r["price"]
        else:
            break
    return (float(price) / base - 1) * 100 if base > 0 else 0.0

def fit_lines(lines, empty: str, limit: int = 1000) -> str:
    """Склеивает строки в значение поля эмбеда (лимит Discord — 1024 символа)."""
    if not lines:
        return empty
    out, used = [], 0
    for i, line in enumerate(lines):
        extra = len(line) + 1
        if used + extra > limit - 25:
            out.append(f"… и ещё {len(lines) - i}")
            break
        out.append(line)
        used += extra
    return "\n".join(out)


class StockTickerButton(discord.ui.Button):
    def __init__(self, ticker: str):
        super().__init__(label=ticker, style=discord.ButtonStyle.secondary, emoji="📈")
        self.ticker = ticker

    async def callback(self, interaction: discord.Interaction):
        view: "StockListView" = self.view
        if interaction.guild_id != view.guild_id:
            return await reply_ephemeral(interaction, "❌ Эта кнопка относится к другому серверу.")
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            payload = await view.cog._build_info(view.guild_id, interaction.user.id,
                                                 interaction.guild.name, self.ticker)
            if payload is None:
                return await interaction.followup.send(f"❌ Акция `{self.ticker}` больше не существует.",
                                                       ephemeral=True)
            embeds, files = payload
            kwargs = {"files": files} if files else {}
            await interaction.followup.send(embeds=embeds, ephemeral=True, **kwargs)
        except Exception:
            log.exception("Stock button failed %s", self.ticker)


class StockListView(discord.ui.View):
    def __init__(self, cog, guild_id: int, tickers):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.message = None
        for t in list(tickers)[:STOCK_LIST_BUTTONS]:
            self.add_item(StockTickerButton(t))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try: await self.message.edit(view=self)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException): pass


# =============================================================================
# БИРЖА
# =============================================================================
class StockCog(commands.Cog):
    def __init__(self, bot: EconomyBot):
        self.bot = bot
        self._last_trade = {}      # (guild_id, user_id) -> monotonic время последней сделки

    async def _require_module(self, ctx) -> bool:
        if await self.bot.module_enabled(ctx.guild.id, "stocks"):
            return True
        await send_embed(ctx, "warning", "📈 Биржа отключена",
                         "Администратор сервера выключил этот модуль. "
                         "Включить можно через `/settings`.")
        return False

    def _throttle(self, gid: int, uid: int) -> float:
        """0 — сделка разрешена (и время зафиксировано); иначе секунд до следующей."""
        t, key = time.monotonic(), (int(gid), int(uid))
        if len(self._last_trade) > 5000:
            self._last_trade = {k: v for k, v in self._last_trade.items()
                                if t - v < STOCK_TRADE_COOLDOWN}
        left = STOCK_TRADE_COOLDOWN - (t - self._last_trade.get(key, -1e9))
        if left > 0:
            return left
        self._last_trade[key] = t
        return 0

    @staticmethod
    def _parse_ticker(raw) -> str:
        return "".join(str(raw or "").split()).upper()

    @staticmethod
    def _looks_like_ticker(raw) -> bool:
        raw = str(raw or "").strip()
        return raw == raw.upper() and bool(STOCK_TICKER_RE.match(raw))

    @staticmethod
    def _auto_ticker_base(name: str) -> str:
        return re.sub(r"[^A-ZА-ЯЁ0-9]", "", str(name).upper())[:STOCK_AUTO_TICKER_LEN]

    async def _free_ticker(self, gid: int, base: str, session=None):
        cands = [base]
        for n in range(2, 100):
            s = str(n)
            cands.append(base[:6 - len(s)] + s)
        taken = {d["ticker"] async for d in self.bot.stocks.find(
            {"guild_id": int(gid), "ticker": {"$in": cands}}, {"ticker": 1}, session=session)}
        return next((c for c in cands if c not in taken), None)

    async def _get_stock(self, gid: int, ticker: str):
        return await self.bot.stocks.find_one({"_id": stock_sid(gid, ticker), "guild_id": int(gid)})

    # ---------- группа ----------
    @commands.hybrid_group(name="stock", aliases=["stocks"], description="Биржа", invoke_without_command=True)
    async def stock(self, ctx):
        if not await self._require_module(ctx): return
        await self._list(ctx)

    @stock.command(name="list", description="Все акции сервера")
    async def stock_list(self, ctx):
        if not await self._require_module(ctx): return
        await self._list(ctx)

    async def _list(self, ctx):
        gid = ctx.guild.id
        docs = await self.bot.stocks.find({"guild_id": gid}).to_list(500)
        if not docs:
            return await send_embed(ctx, "info", "📈 Биржа",
                f"Пока нет ни одной акции.\n`!stock create [ТИКЕР] Название` — IPO за {fmt(STOCK_CREATE_COST)}.")
        # в списке — по капитализации, кнопки — 5 самых ДОРОГИХ акций (по цене)
        by_price = sorted(docs, key=lambda d: float(d["price"]), reverse=True)
        docs.sort(key=lambda d: float(d["price"]) * int(d.get("supply", 0)), reverse=True)
        lines = []
        for d in docs[:15]:
            rows = stock_rows(d)
            ch = stock_change_24h(rows, float(d["price"]))
            lines.append(f"**{d['ticker']}** — {d['name']}: `{fmt_price(d['price'])}` ({ch:+.1f}%)")
        view = StockListView(self, gid, [d["ticker"] for d in by_price[:STOCK_LIST_BUTTONS]])
        embed = make_embed("info", "📈 Биржа", "\n".join(lines),
                           footer="Кнопки — 5 самых дорогих акций • !stock info ТИКЕР")
        view.message = await safe_send(ctx, embed=embed, view=view)

    # ---------- IPO ----------
    @stock.command(name="create", description="Выпустить акции своей компании (IPO)")
    @app_commands.describe(ticker="Тикер (2–6 символов). Пусто — создам из названия",
                           name="Название компании")
    async def stock_create(self, ctx, ticker: Optional[str] = None, *, name: Optional[str] = None):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx): return
        gid, uid = ctx.guild.id, ctx.author.id
        ticker_raw, name_raw = ticker, name
        if ctx.interaction is None:
            # prefix: !stock create [ТИКЕР] Название …
            if name_raw is None:                     # одно слово/фраза в кавычках — это название
                name_raw, ticker_raw = ticker_raw, None
            elif ticker_raw and not self._looks_like_ticker(ticker_raw):
                name_raw, ticker_raw = f"{ticker_raw} {name_raw}", None   # первое слово — часть названия
        name = " ".join(str(name_raw or "").split())
        if not 2 <= len(name) <= 40:
            return await send_embed(ctx, "danger", "Неверное название",
                "Название: от 2 до 40 символов.\nПример: `!stock create Корпорация Зла` "
                "или `!stock create KORP Корпорация Зла`.")
        auto = not (ticker_raw or "").strip()
        base = None
        if auto:
            base = self._auto_ticker_base(name)
            if len(base) < 2:
                return await send_embed(ctx, "danger", "Не удалось создать тикер",
                                        "В названии мало букв — укажи тикер вручную (2–6 символов).")
        else:
            ticker = self._parse_ticker(ticker_raw)
            if not STOCK_TICKER_RE.match(ticker):
                return await send_embed(ctx, "danger", "Неверный тикер",
                                        "Тикер: 2–6 символов, только буквы и цифры.")
        created = now()
        for attempt in range(3):
            try:
                async with await self.bot.mongo.start_session() as session:
                    async with session.start_transaction():
                        if auto:
                            ticker = await self._free_ticker(gid, base, session)
                            if ticker is None:
                                raise EconomyError("Не удалось подобрать свободный тикер — укажи его вручную.")
                        sid = stock_sid(gid, ticker)
                        if await self.bot.stocks.find_one({"_id": sid, "guild_id": gid}, session=session):
                            raise EconomyError(f"Тикер `{ticker}` уже занят на этом сервере.")
                        if not await self.bot.change_cash(gid, uid, -STOCK_CREATE_COST, session=session):
                            raise EconomyError(f"Для IPO нужно `{fmt(STOCK_CREATE_COST)}` наличными.")
                        # деньги «сгорают» в резерв ЦБ — масса не меняется
                        await self.bot.change_reserve(gid, STOCK_CREATE_COST, session=session)
                        await self.bot.stocks.insert_one({
                            "_id": sid, "guild_id": gid, "ticker": ticker, "name": name,
                            "owner_id": uid, "price": STOCK_START_PRICE, "supply": STOCK_START_SUPPLY,
                            "history": [STOCK_START_PRICE], "history_ts": [created],
                            "last_tick": created, "created_at": created}, session=session)
                        await self.bot.stock_holdings.insert_one({
                            **hkey(gid, uid, ticker), "shares": STOCK_START_SUPPLY,
                            "updated_at": created}, session=session)
                        await self.bot.journal(f"user_{uid}", "central_bank", STOCK_CREATE_COST,
                                               f"IPO: выпуск акций {ticker}",
                                               {"type": "stock_ipo", "ticker": ticker},
                                               session=session, guild_id=gid)
                break
            except DuplicateKeyError:
                if auto and attempt < 2:      # гонка за тот же авто-тикер — подбираем заново
                    continue
                return await send_embed(ctx, "danger", "Ошибка", f"Тикер `{ticker}` уже занят.")
            except EconomyError as e:
                return await send_embed(ctx, "danger", "IPO не состоялось", str(e))
        else:
            return await send_embed(ctx, "danger", "IPO не состоялось", "Не удалось подобрать тикер. Попробуй ещё раз.")
        await send_embed(ctx, "success", f"🚀 IPO: {ticker}",
            description=f"Компания **{name}** вышла на биржу.",
            fields=[("🏷️ Тикер", f"`{ticker}`" + (" (авто)" if auto else ""), True),
                    ("💵 Цена акции", f"`{fmt_price(STOCK_START_PRICE)}`", True),
                    ("📦 Твои акции", f"`{fmt(STOCK_START_SUPPLY)}`", True),
                    ("🔥 Сгорело в резерв ЦБ", f"`{fmt(STOCK_CREATE_COST)}`", True)],
            thumbnail=ctx.author.display_avatar.url)

    # ---------- мои акции ----------
    @stock.command(name="my", description="Мои акции и выпущенные компании на этом сервере")
    async def stock_my(self, ctx):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx): return
        gid, uid = ctx.guild.id, ctx.author.id
        holds = await self.bot.stock_holdings.find(
            {"guild_id": gid, "user_id": uid, "shares": {"$gt": 0}}).to_list(500)
        issued = await self.bot.stocks.find(
            {"guild_id": gid, "owner_id": uid}).sort("created_at", 1).to_list(100)
        tickers = [h["ticker"] for h in holds]
        stocks = {}
        if tickers:
            async for d in self.bot.stocks.find({"guild_id": gid, "ticker": {"$in": tickers}}):
                stocks[d["ticker"]] = d
        bought, total = [], 0.0
        for h in holds:
            st = stocks.get(h["ticker"])
            if not st or int(st["owner_id"]) == uid:     # свои выпущенные акции — не «купленные»
                continue
            shares = int(h["shares"])
            value = shares * float(st["price"])
            total += value
            bought.append((value, f"**{st['ticker']}** — {st['name']}: `{fmt(shares)}` шт. ≈ `{fmt(value)}`"))
        bought.sort(key=lambda x: x[0], reverse=True)
        bought_lines = [line for _v, line in bought]
        issued_lines = [f"**{d['ticker']}** — {d['name']}" for d in issued]
        fields = [("📦 Купленные акции", fit_lines(bought_lines, "Пока ничего не куплено."), False)]
        if bought_lines:
            fields.append(("💼 Стоимость портфеля", f"`{fmt(total)}`", True))
        fields.append(("🏢 Выпущенные компании", fit_lines(issued_lines, "Ты не выпускал акции."), False))
        await send_embed(ctx, "info", "📈 Мои акции", fields=fields,
                         footer=f"Сервер: {ctx.guild.name}",
                         thumbnail=ctx.author.display_avatar.url)

    # ---------- торговля ----------
    @commands.hybrid_command(name="buy", description="Купить акции")
    @app_commands.describe(ticker="Тикер", amount="Количество акций")
    async def stock_buy(self, ctx, ticker: str, amount: int):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx): return
        gid, uid = ctx.guild.id, ctx.author.id
        ticker = self._parse_ticker(ticker)
        if amount <= 0 or amount > 1_000_000:
            return await send_embed(ctx, "danger", "Ошибка", "Количество должно быть положительным.")
        left = self._throttle(gid, uid)
        if left > 0:
            return await send_embed(ctx, "warning", "Не так быстро",
                                    f"Следующая сделка через **{fmt_duration(left)}**.")
        sid = stock_sid(gid, ticker)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    st = await self.bot.stocks.find_one({"_id": sid, "guild_id": gid}, session=session)
                    if not st:
                        raise EconomyError(f"Акция `{ticker}` не найдена.")
                    owner = int(st["owner_id"])
                    if owner == uid:
                        raise EconomyError("Нельзя торговать акциями собственной компании.")
                    held = await self.bot.stock_holdings.find_one(hkey(gid, owner, ticker), session=session)
                    available = int((held or {}).get("shares", 0))
                    if amount > available:
                        raise EconomyError(f"У эмитента в продаже только `{fmt(available)}` акций.")
                    # Покупка поднимает цену на 1%; исполняем по цене ПОСЛЕ сдвига,
                    # поэтому цикл «купил-продал» никогда не даёт прибыли.
                    new_price = max(1.0, round(float(st["price"]) * (1 + STOCK_TRADE_STEP), 2))
                    cost = math.ceil(new_price * amount)
                    upd = await self.bot.stocks.update_one(
                        {"_id": sid, "guild_id": gid, "price": st["price"]},
                        {"$set": {"price": new_price, "updated_at": now()}}, session=session)
                    if upd.modified_count != 1:
                        raise EconomyError("Цена изменилась во время сделки. Повтори команду.")
                    await self.bot.ensure_user(gid, owner, session=session)
                    paid = await self.bot.db_users.update_one(
                        {**ukey(gid, uid), "cash": {"$gte": cost}},
                        {"$inc": {"cash": -cost}, "$set": {"updated_at": now()}}, session=session)
                    if paid.modified_count != 1:
                        raise EconomyError(f"Недостаточно наличных. Нужно `{fmt(cost)}`.")
                    # деньги идут владельцу акции
                    await self.bot.db_users.update_one(
                        ukey(gid, owner),
                        {"$inc": {"cash": cost}, "$set": {"updated_at": now()}}, session=session)
                    took = await self.bot.stock_holdings.update_one(
                        {**hkey(gid, owner, ticker), "shares": {"$gte": amount}},
                        {"$inc": {"shares": -amount}, "$set": {"updated_at": now()}}, session=session)
                    if took.modified_count != 1:
                        raise EconomyError("Акции эмитента уже раскуплены.")
                    await self.bot.stock_holdings.update_one(
                        hkey(gid, uid, ticker),
                        {"$inc": {"shares": amount}, "$set": {"updated_at": now()}},
                        upsert=True, session=session)
                    await self.bot.journal(f"user_{uid}", f"user_{owner}", cost,
                                           f"Покупка акций {ticker}",
                                           {"type": "stock_buy", "ticker": ticker,
                                            "shares": amount, "price": new_price},
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Покупка не удалась", str(e))
        await send_embed(ctx, "success", f"📈 Куплено: {ticker}",
            fields=[("📦 Акций", f"`{fmt(amount)}`", True),
                    ("💵 Цена за акцию", f"`{fmt_price(new_price)}`", True),
                    ("💸 Итого", f"`{fmt(cost)}`", True)],
            footer=f"Деньги получил владелец: {st['name']}")

    @commands.hybrid_command(name="sell", description="Продать акции")
    @app_commands.describe(ticker="Тикер", amount="Количество или all")
    async def stock_sell(self, ctx, ticker: str, amount: str):
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        if not await self._require_module(ctx): return
        gid, uid = ctx.guild.id, ctx.author.id
        ticker = self._parse_ticker(ticker)
        mine = await self.bot.stock_holdings.find_one(hkey(gid, uid, ticker))
        qty = parse_amount(amount, int((mine or {}).get("shares", 0)))
        if not qty:
            return await send_embed(ctx, "danger", "Ошибка",
                                    "Укажи положительное количество акций или `all`.")
        left = self._throttle(gid, uid)
        if left > 0:
            return await send_embed(ctx, "warning", "Не так быстро",
                                    f"Следующая сделка через **{fmt_duration(left)}**.")
        sid = stock_sid(gid, ticker)
        try:
            async with await self.bot.mongo.start_session() as session:
                async with session.start_transaction():
                    st = await self.bot.stocks.find_one({"_id": sid, "guild_id": gid}, session=session)
                    if not st:
                        raise EconomyError(f"Акция `{ticker}` не найдена.")
                    owner = int(st["owner_id"])
                    if owner == uid:
                        raise EconomyError("Нельзя торговать акциями собственной компании.")
                    # Продажа понижает цену на 1% (не ниже 1); исполняем по цене после сдвига.
                    new_price = max(1.0, round(float(st["price"]) * (1 - STOCK_TRADE_STEP), 2))
                    proceeds = max(1, math.floor(new_price * qty))
                    upd = await self.bot.stocks.update_one(
                        {"_id": sid, "guild_id": gid, "price": st["price"]},
                        {"$set": {"price": new_price, "updated_at": now()}}, session=session)
                    if upd.modified_count != 1:
                        raise EconomyError("Цена изменилась во время сделки. Повтори команду.")
                    await self.bot.ensure_user(gid, owner, session=session)
                    taken = await self.bot.stock_holdings.update_one(
                        {**hkey(gid, uid, ticker), "shares": {"$gte": qty}},
                        {"$inc": {"shares": -qty}, "$set": {"updated_at": now()}}, session=session)
                    if taken.modified_count != 1:
                        raise EconomyError("У тебя недостаточно акций.")
                    # Выкупает эмитент: платит из своих наличных, акции возвращаются к нему.
                    paid = await self.bot.db_users.update_one(
                        {**ukey(gid, owner), "cash": {"$gte": proceeds}},
                        {"$inc": {"cash": -proceeds}, "$set": {"updated_at": now()}}, session=session)
                    if paid.modified_count != 1:
                        raise EconomyError("У эмитента недостаточно наличных для выкупа.")
                    await self.bot.ensure_user(gid, uid, session=session)
                    await self.bot.db_users.update_one(
                        ukey(gid, uid),
                        {"$inc": {"cash": proceeds}, "$set": {"updated_at": now()}}, session=session)
                    await self.bot.stock_holdings.update_one(
                        hkey(gid, owner, ticker),
                        {"$inc": {"shares": qty}, "$set": {"updated_at": now()}},
                        upsert=True, session=session)
                    await self.bot.journal(f"user_{owner}", f"user_{uid}", proceeds,
                                           f"Продажа акций {ticker}",
                                           {"type": "stock_sell", "ticker": ticker,
                                            "shares": qty, "price": new_price},
                                           session=session, guild_id=gid)
        except EconomyError as e:
            return await send_embed(ctx, "danger", "Продажа не удалась", str(e))
        await send_embed(ctx, "success", f"📉 Продано: {ticker}",
            fields=[("📦 Акций", f"`{fmt(qty)}`", True),
                    ("💵 Цена за акцию", f"`{fmt_price(new_price)}`", True),
                    ("💰 Получено", f"`{fmt(proceeds)}`", True)])

    # ---------- профиль и графики ----------
    @stock.command(name="info", description="Профиль акции и графики")
    @app_commands.describe(ticker="Тикер")
    async def stock_info(self, ctx, ticker: str):
        if not await self._require_module(ctx): return
        async with ctx.typing():
            await self._info(ctx, self._parse_ticker(ticker))

    async def _info(self, ctx, ticker):
        payload = await self._build_info(ctx.guild.id, ctx.author.id, ctx.guild.name, ticker)
        if payload is None:
            return await send_embed(ctx, "danger", "Акция не найдена", f"Тикер `{ticker}` не существует.")
        embeds, files = payload
        kwargs = {"files": files} if files else {}
        await safe_send(ctx, embeds=embeds, **kwargs)

    async def _build_info(self, gid, uid, guild_name, ticker):
        """Профиль акции: (embeds, files) или None. Всегда в рамках сервера gid."""
        st = await self._get_stock(gid, ticker)
        if not st:
            return None
        price = float(st["price"])
        rows = stock_rows(st)
        change = stock_change_24h(rows, price)
        cap = price * int(st.get("supply", 0))
        mine = await self.bot.stock_holdings.find_one(hkey(gid, uid, ticker))
        color = (discord.Color.green() if change > 0
                 else discord.Color.red() if change < 0 else discord.Color.blurple())
        e24 = make_embed("info", f"{ticker} — {st['name']}",
            fields=[("💵 Цена", f"`{fmt_price(price)}`", True),
                    ("📊 За 24ч", f"`{change:+.2f}%`", True),
                    ("🏛️ Капитализация", f"`{fmt(cap)}`", True),
                    ("👤 Владелец", f"<@{int(st['owner_id'])}>", True),
                    ("📦 Всего акций", f"`{fmt(st.get('supply', 0))}`", True),
                    ("🧾 У тебя", f"`{fmt((mine or {}).get('shares', 0))}`", True)],
            footer=f"Сервер: {guild_name}")
        e24.title = f"{'📈' if change >= 0 else '📉'} {ticker} — {st['name']}"
        e24.color = color
        cutoff = now() - timedelta(hours=24)
        first = next((i for i, r in enumerate(rows) if r["created_at"] >= cutoff), len(rows) - 1)
        day = rows[max(0, first - 1):]
        files = []
        try:
            buf24, buf_all = io.BytesIO(), io.BytesIO()
            async with self.bot.chart_lock:
                await asyncio.to_thread(EconomyBot._render_chart, day[-1500:], buf24, "price", 2, "%H:%M")
                await asyncio.to_thread(EconomyBot._render_chart, rows[-2500:], buf_all, "price", 2, "%d.%m")
            buf24.seek(0); buf_all.seek(0)
            files = [discord.File(buf24, filename="stock_24h.png"),
                     discord.File(buf_all, filename="stock_all.png")]
        except Exception:
            log.exception("Stock chart render failed %s", ticker)
        if not files:
            return [e24], []
        e24.set_image(url="attachment://stock_24h.png")
        eall = discord.Embed(title=f"{ticker} — вся история", color=color)
        eall.set_image(url="attachment://stock_all.png")
        return [e24, eall], files


# =============================================================================
# ИИ (управляется из панели /settings)
# =============================================================================
async def guild_ai_enabled(bot, guild_id: int) -> bool:
    doc = await bot.settings.find_one({"_id": int(guild_id)}) or {}
    return bool(doc.get("ai_enabled", True))


# =============================================================================
# ПАНЕЛЬ НАСТРОЕК /settings
# =============================================================================
def preset_label(preset):
    return PRESETS[preset]["label"] if preset in PRESETS else "⚙️ Кастомная"

def settings_embed(guild, cfg):
    colors = {"easy": discord.Color.green(), "standard": discord.Color.gold(),
              "hardcore": discord.Color.red()}
    embed = discord.Embed(
        title=f"⚙️ Настройки экономики — {guild.name}",
        description=(f"Текущий пресет: **{preset_label(cfg['preset'])}**\n\n"
                     "Выбери пресет в меню ниже. «⚙️ Кастомная» откроет ручную настройку."),
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
    # Модули
    mods = cfg.get("modules", {})
    mod_lines = []
    for key, label in [("business", "💼 Бизнес"), ("casino", "🎰 Казино"), ("crime", "🥷 Криминал"),
                       ("stocks", "📈 Биржа"), ("daily", "🎁 Ежедневный бонус")]:
        state = "✅ Вкл" if mods.get(key, True) else "❌ Выкл"
        mod_lines.append(f"{label}: {state}")
    embed.add_field(name="🧩 Модули", value="\n".join(mod_lines), inline=False)
    texts = cfg.get("custom_texts") or {}
    embed.add_field(
        name="✏️ Кастомные тексты",
        value="\n".join(f"{label}: {'✅ свой' if texts.get(key) else '▫️ стандартный'}"
                        for key, label in CUSTOM_TEXT_LABELS.items()),
        inline=False)
    embed.set_footer(text="Изменения применяются сразу. У каждого сервера своя изолированная экономика.")
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


class ModuleToggleButton(discord.ui.Button):
    def __init__(self, module: str, label: str, emoji: str):
        self.module = module
        self.emoji_str = emoji
        super().__init__(label=label, style=discord.ButtonStyle.secondary, emoji=emoji)

    async def callback(self, interaction: discord.Interaction):
        cfg = await self.view.bot.get_settings(self.view.guild.id)
        mods = dict(cfg.get("modules", {}))
        current = mods.get(self.module, True)
        mods[self.module] = not current
        await self.view.bot.settings.update_one(
            {"_id": self.view.guild.id},
            {"$set": {"modules": mods, "updated_at": now()}},
            upsert=True,
        )
        self.view.bot.invalidate_settings(self.view.guild.id)
        await self.view.refresh(interaction)


class AIReportButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="ИИ-отчёт", emoji="🤖", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        bot, guild = self.view.bot, self.view.guild
        if not bot.ai.enabled:
            return await send_embed(interaction, "danger", "🤖 ИИ отключён",
                                    "У бота не задан `GROQ_API_KEY`.", ephemeral=True)
        if not await guild_ai_enabled(bot, guild.id):
            return await send_embed(interaction, "warning", "🤖 ИИ-отчёты выключены",
                                    "Включи их кнопкой «Вкл/Выкл ИИ» в этой панели.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stats = await bot.stats(guild.id)
            text = await bot.ai.market_report(stats)
        except AIError as e:
            return await send_embed(interaction, "danger", "⚠️ Ошибка ИИ", str(e), ephemeral=True)
        except Exception:
            log.exception("Ошибка генерации ИИ-отчёта")
            return await send_embed(interaction, "danger", "⚠️ Ошибка ИИ",
                                    "Не удалось сформировать отчёт.", ephemeral=True)
        if len(text) > 1900:
            text = text[:1900].rstrip() + "…"
        await send_embed(interaction, "info", "🤖 ИИ-отчёт по экономике",
                         description=text,
                         thumbnail=guild.icon.url if guild.icon else None,
                         ephemeral=True)


class AIToggleButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        super().__init__(label="Вкл/Выкл ИИ", emoji="🧠",
                         style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        bot, guild = self.view.bot, self.view.guild
        new_state = not await guild_ai_enabled(bot, guild.id)
        await bot.settings.update_one(
            {"_id": guild.id},
            {"$set": {"ai_enabled": new_state, "updated_at": now()}},
            upsert=True)
        bot.invalidate_settings(guild.id)
        log.info("Guild %s: ИИ -> %s", guild.id, new_state)
        await self.view.refresh(interaction)
        if new_state and not bot.ai.enabled:
            await reply_ephemeral(interaction, "⚠️ Включено, но у бота не задан `AI_API_KEY`/`GROQ_API_KEY` — ИИ недоступен.")


class CustomTextsModal(discord.ui.Modal):
    """Свои тексты для !crime / !slut. Пустое поле = стандартный текст."""
    def __init__(self, panel, current: dict):
        super().__init__(title="Кастомные тексты", timeout=300)
        self.panel = panel
        self.inputs = {}
        for key, label in CUSTOM_TEXT_LABELS.items():
            item = discord.ui.TextInput(
                label=label[:45], style=discord.TextStyle.paragraph,
                default=(current.get(key) or None), required=False,
                placeholder="Пусто — стандартный текст", max_length=CUSTOM_TEXT_MAX)
            self.add_item(item)
            self.inputs[key] = item

    async def on_submit(self, interaction: discord.Interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and perms.administrator) or interaction.guild_id != self.panel.guild.id:
            return await reply_ephemeral(interaction, "❌ Нужны права администратора.")
        texts = {key: item.value.strip() for key, item in self.inputs.items() if item.value.strip()}
        gid = self.panel.guild.id
        await self.panel.bot.settings.update_one(
            {"_id": gid}, {"$set": {"custom_texts": texts, "updated_at": now()}}, upsert=True)
        self.panel.bot.invalidate_settings(gid)
        log.info("Guild %s: кастомные тексты обновлены (%s шт.)", gid, len(texts))
        await self.panel.refresh(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Ошибка формы кастомных текстов", exc_info=error)
        await reply_ephemeral(interaction, "⚠️ Внутренняя ошибка. Попробуй ещё раз.")


class CustomTextsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Кастомные тексты", emoji="✏️", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and perms.administrator):
            return await reply_ephemeral(interaction, "❌ Только для администраторов.")
        cfg = await view.bot.get_settings(view.guild.id)
        await interaction.response.send_modal(CustomTextsModal(view, cfg.get("custom_texts") or {}))


class SettingsView(discord.ui.View):
    def __init__(self, bot, author_id: int, guild: discord.Guild, cfg: dict, ai_enabled: bool = True):
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = author_id
        self.guild = guild
        self.message = None
        self.add_item(PresetSelect(cfg["preset"]))
        if cfg["preset"] == "custom":
            self.add_item(SectionSelect())
        # Кнопки-переключатели модулей
        mods = cfg.get("modules", {})
        for module, label, emoji in [("business", "Бизнес", "💼"),
                                      ("casino", "Казино", "🎰"),
                                      ("crime", "Криминал", "🥷"),
                                      ("stocks", "Биржа", "📈"),
                                      ("daily", "Бонус", "🎁")]:
            btn = ModuleToggleButton(module, label, emoji)
            btn.style = discord.ButtonStyle.success if mods.get(module, True) else discord.ButtonStyle.secondary
            self.add_item(btn)
        self.add_item(AIReportButton())
        self.add_item(AIToggleButton(ai_enabled))
        self.add_item(CustomTextsButton())

    async def interaction_check(self, interaction: discord.Interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id != self.author_id or not (perms and perms.administrator):
            await reply_ephemeral(interaction, "❌ Эта панель доступна только администратору, открывшему её.")
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        cfg = await self.bot.get_settings(self.guild.id)
        ai_on = await guild_ai_enabled(self.bot, self.guild.id)
        new_view = SettingsView(self.bot, self.author_id, self.guild, cfg, ai_on)
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
            update = {"$set": {"eco.preset": "custom", "updated_at": now()}}
        else:
            update = {
                "$set": {"eco": {"preset": choice, **PRESETS[choice]["values"]}, "updated_at": now()},
                "$unset": {f"cooldown_{key}": "" for key in COOLDOWN_KEYS},
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
        if ctx.guild is None:
            return await send_embed(ctx, "danger", "Ошибка", "Команда доступна только на сервере.")
        cfg = await self.bot.get_settings(ctx.guild.id)
        ai_on = await guild_ai_enabled(self.bot, ctx.guild.id)
        view = SettingsView(self.bot, ctx.author.id, ctx.guild, cfg, ai_on)
        view.message = await safe_send(ctx, embed=settings_embed(ctx.guild, cfg), view=view, ephemeral=True)


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================
def main():
    check_env()
    bot = EconomyBot()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
