# Economy Bot

Discord-бот с полноценной экономикой для серверов: у каждого сервера свой баланс, своя инфляция и свой Центральный банк. Включает казино, фондовую биржу, бизнесы, кредиты и ежедневные ИИ-отчёты об экономике.

## Функционал

- 💰 **Экономика** — баланс (наличные + банк), переводы, `!work`, `!crime`, `!slut`, `!daily`, `!rob`, топ игроков
- 🏦 **Банк и Центральный банк** — вклады и снятие, эмиссия и сжигание денег, инфляция, статистика экономики сервера (`!economy` и `!cb` — одна и та же сводка)
- 🏢 **Бизнесы** — покупка, улучшение уровней, почасовой доход
- 💳 **Кредиты** — выдача и погашение с ограничением по сроку
- 🎰 **Казино** — слоты, блэкджек, русская рулетка (соло и с друзьями), статистика игрока
- 📈 **Биржа** — IPO собственных компаний, покупка и продажа акций, графики, список акций
- 🤖 **ИИ-отчёты** — ежедневный анализ экономики сервера через OpenAI-совместимый API (по умолчанию Groq)
- 📊 **Графики** — автоматические и по запросу (`!graphs`), отправка в выбранный канал
- ⚙️ **Настройки** — панель `/settings` для администраторов (пресеты, кулдауны и прочие значения вручную, кастомные тексты, ИИ)
- 🌐 **Health-check** — HTTP-эндпоинты `/` и `/health` для хостингов
- Все команды работают и как префиксные (`!`), и как слеш-команды

## Стек технологий

- Python 3
- [discord.py](https://github.com/Rapptz/discord.py) — Discord API
- MongoDB (Atlas) + [Motor](https://motor.readthedocs.io/) / PyMongo — база данных
- aiohttp — HTTP-сервер health-check и запросы к ИИ API
- matplotlib — построение графиков
- Groq / любой OpenAI-совместимый API — ИИ-отчёты

## Установка

```bash
git clone <repo-url>
cd economy-bot-V1
pip install -r requirements.txt
```

## Переменные окружения (ENV)

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Токен Discord-бота |
| `MONGO_USER` | ✅ | — | Пользователь MongoDB |
| `MONGO_PASSWORD` | ✅ | — | Пароль MongoDB |
| `MONGO_DB_NAME` | ❌ | `economy_bot` | Название базы данных |
| `PORT` | ❌ | `10000` | Порт HTTP health-check сервера |
| `AI_API_KEY` | ❌ | — | Ключ ИИ API (без него ИИ-функции недоступны) |
| `AI_URL` | ❌ | `https://api.groq.com/openai/v1/chat/completions` | Эндпоинт ИИ API |
| `AI_MODEL` | ❌ | `llama-3.3-70b-versatile` | Модель ИИ |

Пример `.env` / настроек хостинга:

```env
DISCORD_TOKEN=your_discord_bot_token
MONGO_USER=your_mongo_user
MONGO_PASSWORD=your_mongo_password
MONGO_DB_NAME=economy_bot
PORT=10000
AI_API_KEY=your_ai_key
```

> Бот не читает `.env` сам — задайте переменные в окружении (хостинг, `export`) или подгрузите файл любым удобным способом.

## Запуск

```bash
python bot.py
```

При старте бот проверяет обязательные переменные (`DISCORD_TOKEN`, `MONGO_USER`, `MONGO_PASSWORD`) и подключается к MongoDB. Статус активности: `!balance | !cb`.

## Discord Intents

Бот запрашивает только `guilds`, `guild_messages` и `message_content` (нужен для префиксных команд `!`). **Server Members Intent не нужен** — имена участников подгружаются через `fetch_member`. Следствие: в префиксных командах игрока лучше указывать упоминанием (`@user`); поиск по имени или ID без Members Intent может не работать (слеш-команды не затронуты).

## Алиасы команд

| Команда | Алиасы |
|---|---|
| `!balance` | `!bal` |
| `!deposit` / `!withdraw` | `!dep` / `!with` |
| `!business` | `!biz` |
| `!casino-stats` | `!cstats` |
| `!rr` / `!bj` | `!roulette` / `!blackjack` |
| `!stock` | `!stocks` |
| `!graphs` | `!chart` |
| `!economy` | `!cb` (отдельная команда с тем же содержимым; работает и как `/cb`) |

Однобуквенные алиасы (`!b`, `!p`, `!w`, `!c`, `!s`, `!d`, `!r`, `!t`, `!h`) и `!ln`, `!sl`, `!cs` удалены. Команда `!set-cooldown` удалена — кулдауны настраиваются в `/settings`.

## Транзакции

Все денежные операции выполняются в транзакциях MongoDB через `EconomyBot.run_tx`. Временные ошибки (`TransientTransactionError`, например write-conflict на документе резерва ЦБ) и неизвестный результат коммита обрабатываются автоматически: транзакция перезапускается (до 5 попыток с короткой паузой). Бизнес-ошибки (`EconomyError`: не хватает средств, кулдаун и т.д.) откатывают транзакцию без повторов.

## Проверка БД

Миграции удалены из кода (изоляция по `guild_id` проверена по всем запросам). Два чтения-fallback для старых документов настроек оставлены намеренно — миграция их никогда не переписывала. Если перед их удалением нужно убедиться, что они не нужны, выполните в `mongosh`:

```js
// 1) документы со старыми top-level кулдаунами (вместо eco.cooldown_*)
db.guild_settings.countDocuments({ $or: [
  { cooldown_work: { $exists: true } }, { cooldown_crime: { $exists: true } },
  { cooldown_slut: { $exists: true } }, { cooldown_rob: { $exists: true } },
  { cooldown_daily: { $exists: true } } ] })

// 2) документы с пресетом, но без eco.inflation_multiplier
db.guild_settings.countDocuments({ "eco.preset": { $exists: true },
                                    "eco.inflation_multiplier": { $exists: false } })

// 3) нет ли старого уникального индекса только по user_id (мешает мультисерверности)
db.users.getIndexes(); db.businesses.getIndexes(); db.casino_stats.getIndexes()
```

Если оба счётчика равны `0`, а в индексах нет уникального ключа `{ user_id: 1 }` без `guild_id`, fallback-чтение в `EconomyBot.get_settings` можно удалить.
