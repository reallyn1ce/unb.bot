"""Награды !crime / !slut: настройки, диапазон, резерв ЦБ, кулдаун, изоляция серверов."""
from common import *

def force(success):
    B.rng = types.SimpleNamespace(random=lambda: 0.0 if success else 0.99,
                                  randint=random.randint, choice=random.choice)

async def set_eco(bot, gid, **vals):
    await bot.settings.update_one({"_id": gid}, {"$set": {"eco": {"preset": "custom", **vals}}}, upsert=True)
    bot.invalidate_settings(gid)

async def main():
    bot = FakeBot(); eco = B.EconomyCog(bot)
    for g in (1, 2): await bot.ensure_guild_funds(g)
    for g in (1, 2): await bot.change_reserve(g, 100_000)
    for g in (1, 2): await bot.ensure_user(g, 5)

    # --- 1. значения по умолчанию = прежние константы, у пресетов тоже
    cfg = await bot.get_settings(1)
    assert (cfg["crime_min"], cfg["crime_max"], cfg["slut_min"], cfg["slut_max"]) == (300, 900, 200, 700)
    assert all(p["values"]["crime_min"] == 300 and p["values"]["slut_max"] == 700 for p in B.PRESETS.values())
    print("T1 значения по умолчанию совпадают с прежними константами")

    # --- 2. валидация ввода (отрицательные, мусор, min > max)
    for key, bad in (("crime_min", "-1"), ("crime_max", "-500"), ("slut_min", "abc"), ("slut_max", "inf"),
                     ("crime_min", ""), ("slut_max", "1000001")):
        try: B.parse_setting(key, bad); raise SystemExit(f"FAIL {key}={bad!r}")
        except ValueError: pass
    assert B.parse_setting("crime_min", "0") == 0 and B.parse_setting("slut_max", "1 000") == 1000
    base = dict(await bot.get_settings(1))
    assert B.reward_range_errors({**base, "crime_min": 900, "crime_max": 300}, {"crime_min", "crime_max"})
    assert B.reward_range_errors({**base, "slut_min": -1}, {"slut_min"})
    assert not B.reward_range_errors({**base, "crime_min": 5, "crime_max": 5}, {"crime_min", "crime_max"})
    # затрагивается только своя пара
    assert not B.reward_range_errors({**base, "crime_min": 900, "crime_max": 300}, {"slut_min"})
    assert "rewards" in B.SETTING_SECTIONS and len(B.SETTING_SECTIONS["rewards"]["keys"]) == 4   # лимит модалки: 5
    print("T2 валидация: отрицательные/мусор/inf отклонены, min<=max проверяется")

    # --- 3. награда в заданном диапазоне, из резерва ЦБ, масса денег не меняется
    await set_eco(bot, 1, crime_min=1000, crime_max=1010, slut_min=50, slut_max=60)
    force(True)
    seen = set(); m0 = money(bot, 1)
    for i in range(30):
        c0, r0 = cash(bot, 5, 1), reserve(bot, 1)
        bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
        await eco._criminal_action(Ctx(1, 5), "crime")
        got = cash(bot, 5, 1) - c0
        assert 1000 <= got <= 1010, got
        assert r0 - reserve(bot, 1) == got, "награда должна быть списана из резерва"
        seen.add(got)
    assert len(seen) > 3, "награда должна быть случайной в диапазоне"
    assert money(bot, 1) == m0
    for i in range(20):
        bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_slut"] = None
        c0 = cash(bot, 5, 1); await eco._criminal_action(Ctx(1, 5), "slut")
        assert 50 <= cash(bot, 5, 1) - c0 <= 60
    print("T3 награды в диапазоне, случайны, платятся из резерва; масса денег неизменна")

    # --- 4. min == max -> точная сумма; 0..0 -> без выплат и без ошибок
    await set_eco(bot, 1, crime_min=777, crime_max=777)
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    c0 = cash(bot, 5, 1); await eco._criminal_action(Ctx(1, 5), "crime"); assert cash(bot, 5, 1) - c0 == 777
    await set_eco(bot, 1, crime_min=0, crime_max=0)
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    c0, r0 = cash(bot, 5, 1), reserve(bot, 1)
    ctx = Ctx(1, 5); await eco._criminal_action(ctx, "crime")
    assert cash(bot, 5, 1) == c0 and reserve(bot, 1) == r0
    assert user(bot, 5, 1)["last_crime"] is not None, "кулдаун должен сработать и при награде 0"
    # испорченные значения в БД (min > max, отрицательные) не ломают команду и не дают отрицательную награду
    await set_eco(bot, 1, crime_min=900, crime_max=300)
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    c0 = cash(bot, 5, 1); await eco._criminal_action(Ctx(1, 5), "crime"); assert 300 <= cash(bot, 5, 1) - c0 <= 900
    await set_eco(bot, 1, crime_min=-50, crime_max=-10)
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    c0 = cash(bot, 5, 1); await eco._criminal_action(Ctx(1, 5), "crime"); assert cash(bot, 5, 1) == c0
    print("T4 min==max, 0..0, испорченные значения — безопасно")

    # --- 5. кулдаун сохраняется
    await set_eco(bot, 1, crime_min=100, crime_max=100, cooldown_crime=3600)
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    c0 = cash(bot, 5, 1); await eco._criminal_action(Ctx(1, 5), "crime"); assert cash(bot, 5, 1) - c0 == 100
    ctx = Ctx(1, 5); await eco._criminal_action(ctx, "crime")
    assert cash(bot, 5, 1) - c0 == 100, "повтор до кулдауна не должен платить"
    print("T5 кулдаун работает")

    # --- 6. недостаточный резерв: денег не создаём, кулдаун не тратится, состояние не меняется
    await set_eco(bot, 1, crime_min=5000, crime_max=5000, cooldown_crime=0)
    bot.economy.docs[[d["_id"] for d in bot.economy.docs].index(bot.cb_id(1))]["reserve"] = 4999
    bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 1].index(5)]["last_crime"] = None
    snap = bot.db.snapshot(); c0, r0 = cash(bot, 5, 1), reserve(bot, 1)
    ctx = Ctx(1, 5); force(True); await eco._criminal_action(ctx, "crime")
    assert cash(bot, 5, 1) == c0 and reserve(bot, 1) == r0 == 4999
    assert user(bot, 5, 1).get("last_crime") is None, "кулдаун не должен тратиться при пустом резерве"
    assert reserve(bot, 1) >= 0
    assert "резерве ЦБ" in str(ctx.sent[-1].__dict__), ctx.sent[-1].__dict__
    # ровно хватает -> выплата проходит и резерв == 0
    bot.economy.docs[[d["_id"] for d in bot.economy.docs].index(bot.cb_id(1))]["reserve"] = 5000
    await eco._criminal_action(Ctx(1, 5), "crime"); assert reserve(bot, 1) == 0 and cash(bot, 5, 1) - c0 == 5000
    print("T6 нехватка резерва: выплаты нет, кулдаун цел, деньги не создаются")

    # --- 7. изоляция серверов: настройки гильдии 1 не влияют на гильдию 2
    c2 = cash(bot, 5, 2); r2 = reserve(bot, 2); force(True)
    await eco._criminal_action(Ctx(2, 5), "crime")
    got = cash(bot, 5, 2) - c2
    assert 300 <= got <= 900 and r2 - reserve(bot, 2) == got
    print("T7 изоляция серверов ok")

    # --- 8. неудача: штраф идёт в бюджет, награда не выплачивается
    force(False); bot.db_users.docs[[d["user_id"] for d in bot.db_users.docs if d["guild_id"] == 2].index(5)]["last_crime"] = None
    m0 = money(bot, 2); r0 = reserve(bot, 2); await eco._criminal_action(Ctx(2, 5), "crime")
    assert money(bot, 2) == m0 and reserve(bot, 2) == r0
    print("T8 механика неудачи (штраф) не изменилась")
    print("ВСЕ ТЕСТЫ НАГРАД ПРОШЛИ")
asyncio.run(main())
