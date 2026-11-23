"""Панель /settings: форма наград (SettingsModal), права администратора, сохранение, регрессия !work и казино."""
from common import *

async def main():
    bot = FakeBot(); await bot.ensure_guild_funds(G); await bot.change_reserve(G, 500_000)
    refreshed = []
    class Panel:
        def __init__(s): s.bot, s.guild = bot, types.SimpleNamespace(id=G, name="G")
        async def refresh(s, interaction): refreshed.append(1)
    panel = Panel()
    cfg = await bot.get_settings(G)
    modal = B.SettingsModal(panel, "rewards", cfg)
    assert set(modal.inputs) == {"crime_min", "crime_max", "slut_min", "slut_max"} and len(modal.children) == 4
    assert [i.default for i in modal.inputs.values()] == ["300", "900", "200", "700"]

    def inter(admin=True):
        i = FInter(1, FChannel()); i.user.guild_permissions = types.SimpleNamespace(administrator=admin); return i
    def fill(m, cm, cx, sm, sx):
        for k, v in zip(("crime_min", "crime_max", "slut_min", "slut_max"), (cm, cx, sm, sx)): m.inputs[k].value = v

    # не администратор
    fill(modal, "1", "2", "3", "4"); i = inter(False); await modal.on_submit(i)
    assert "администратор" in i.replies[-1] and not refreshed
    assert (await bot.get_settings(G))["crime_min"] == 300
    # отрицательное / min > max / мусор — ничего не сохраняется
    for vals in (("-1", "900", "200", "700"), ("1000", "900", "200", "700"), ("300", "900", "800", "700"),
                 ("abc", "900", "200", "700"), ("300", "900", "200", "-7")):
        fill(modal, *vals); i = inter(); await modal.on_submit(i)
        assert i.replies and i.replies[-1].startswith("❌ Не сохранено"), vals
        c = await bot.get_settings(G); assert (c["crime_min"], c["crime_max"], c["slut_min"], c["slut_max"]) == (300, 900, 200, 700)
    assert not refreshed
    # корректное сохранение
    fill(modal, "10", "20", "30", "40"); i = inter(); await modal.on_submit(i)
    c = await bot.get_settings(G)
    assert (c["crime_min"], c["crime_max"], c["slut_min"], c["slut_max"]) == (10, 20, 30, 40) and c["preset"] == "custom"
    assert refreshed and not i.replies
    # прочие настройки не затронуты
    assert c["work_min"] == cfg["work_min"] and c["tax_rate"] == cfg["tax_rate"]
    # равенство min == max допустимо
    fill(modal, "5", "5", "0", "0"); i = inter(); await modal.on_submit(i)
    c = await bot.get_settings(G); assert (c["crime_min"], c["crime_max"], c["slut_min"], c["slut_max"]) == (5, 5, 0, 0)
    # отображение
    emb = B.settings_embed(types.SimpleNamespace(name="G"), c)
    assert any("Награды" in n for n, _v in emb.fields)
    # соседняя форма (зарплата): min > max по-прежнему отклоняется
    wm = B.SettingsModal(panel, "pay", c); wm.inputs["work_min"].value = "900"; wm.inputs["work_max"].value = "100"
    i = inter(); await wm.on_submit(i); assert "Зарплата" in i.replies[-1]
    print("T1 форма наград: права, валидация, сохранение, отображение ok")

    # ---- регрессия: !work, слот (казино), экономика сходится
    await bot.ensure_user(G, 7); await give(bot, 7, 1000)
    m0 = money(bot)
    eco = B.EconomyCog(bot); ctx = Ctx(G, 7); await eco._work(ctx)
    assert money(bot) == m0 and cash(bot, 7) > 1000          # зарплата из резерва, налог в бюджет
    cog = B.CasinoCog(bot)
    B.rng = types.SimpleNamespace(random=random.random, randint=random.randint, choice=random.choice, shuffle=random.shuffle, choices=random.choices, sample=random.sample)
    for _ in range(15):
        try: await cog.run_game("coinflip" if False else "slot", G, 7, 20)
        except B.EconomyError: break
    assert money(bot) == m0 and not bot.active_games.docs
    print("T2 регрессия: !work и слоты — деньги сохраняются")
    print("ВСЕ ТЕСТЫ ПАНЕЛИ ПРОШЛИ")
asyncio.run(main())
