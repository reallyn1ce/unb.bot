"""Русская рулетка: «Играть снова» — чистый раунд, одно списание, параллельные игроки, старые кнопки."""
from common import *

def force_rng(die=False):
    B.rng = types.SimpleNamespace(random=lambda: 0.0, randint=(lambda a, b: 1 if die else b),
                                  choice=random.choice)

async def start_game(cog, ch, gid, players_bets):
    """Ставки списываются как в игре; возвращает (msg, game)."""
    players = []
    for uid, bet in players_bets:
        cog.active_rr.add((gid, uid))
        sid = await cog.rr_take_stake(gid, uid, bet)
        players.append(B.RRPlayer(uid, f"u{uid}", bet, sid))
    msg = FMsg(); ch.partial[msg.id] = msg
    await cog.rr_begin(msg, gid, players)
    return msg, msg.view

async def shoot(game, uid, ch):
    i = FInter(uid, ch, game.message); await game.btn_shoot.callback(i); return i
async def cashout(game, uid, ch):
    i = FInter(uid, ch, game.message); await game.btn_cash.callback(i); return i

async def main():
    bot = FakeBot(); cog = B.CasinoCog(bot); ch = FChannel()
    await bot.ensure_guild_funds(G); await bot.change_reserve(G, 1_000_000)
    A, Bb, C = 21, 22, 23
    for u in (A, Bb, C): await give(bot, u, 10_000)
    m0 = money(bot)

    # ---- 1. соло: выстрел + «Забрать» -> под итогом кнопка «Играть снова»
    force_rng(die=False)
    msg, game = await start_game(cog, ch, G, [(A, 100)])
    await shoot(game, A, ch); await cashout(game, A, ch)
    assert game.ended and (G, A) not in cog.active_rr
    again = msg.view
    assert isinstance(again, B.RRAgainView) and any(c.label == "Играть снова" for c in again.children)
    assert cash(bot, A) == 10_000 - 100 + 118 and money(bot) == m0
    assert not bot.active_games.docs, "записи ставок должны быть закрыты"
    print("T1 раунд завершён, кнопка «Играть снова» показана, выплата корректна")

    # ---- 2. старые кнопки закончившегося раунда ничего не делают
    snap = bot.db.snapshot(); steps = game.players[0].steps
    i = await shoot(game, A, ch); i2 = await cashout(game, A, ch)
    assert bot.db.snapshot() == snap and game.players[0].steps == steps
    print("T2 старые кнопки раунда не запускают повторных действий")

    # ---- 3. посторонний не может использовать кнопку; нажатие не списывает деньги
    btn = again.btn_again
    i = FInter(C, ch, msg); await btn.callback(i)
    assert i.modal is None and i.replies, "посторонний не должен получить форму"
    c0 = cash(bot, A)
    i = FInter(A, ch, msg); await btn.callback(i)
    assert isinstance(i.modal, B.RRBetModal) and cash(bot, A) == c0
    print("T3 кнопка только для участников; до выбора ставки ничего не списывается")

    # ---- 4. форма: некорректная ставка / слишком большая -> ключ не залипает
    for bad in ("abc", "999999", "0", "-5"):
        m = i.modal; m.bet_input.value = bad
        i2 = FInter(A, ch, msg); await m.on_submit(i2)
        assert i2.replies and i2.sent_msg is None and (G, A) not in cog.active_rr and cash(bot, A) == c0, bad
    print("T4 неверные ставки отклонены, ключ не залип, деньги целы")

    # ---- 5. корректная ставка: форма -> выбор режима (без списания) -> списание ровно один раз
    m = i.modal; m.bet_input.value = "50"
    ia = FInter(A, ch, msg); await m.on_submit(ia)
    mode_view = ia.sent_msg.view
    assert isinstance(mode_view, B.RRModeView) and cash(bot, A) == c0 and (G, A) in cog.active_rr
    # повторная отправка формы и повторное нажатие кнопки — отклонены
    ib = FInter(A, ch, msg); await m.on_submit(ib); assert ib.sent_msg is None and ib.replies
    ic = FInter(A, ch, msg); await btn.callback(ic); assert ic.modal is None and ic.replies
    # «двойной клик» по режиму одновременно -> одно списание
    i1, i2 = FInter(A, ch, ia.sent_msg), FInter(A, ch, ia.sent_msg)
    await asyncio.gather(mode_view.btn_solo.callback(i1), mode_view.btn_solo.callback(i2))
    assert cash(bot, A) == c0 - 50, cash(bot, A) - c0
    assert len(bot.active_games.docs) == 1 and bot.active_games.docs[0]["bet"] == 50
    new_game = ia.sent_msg.view
    assert isinstance(new_game, B.RRGameView) and new_game is not game
    assert new_game.players[0].steps == 0 and new_game.pot == 0 and not new_game.log and not new_game.ended
    assert new_game.players[0].stake_id != game.players[0].stake_id
    print("T5 новый раунд: чистое состояние, ставка списана ровно один раз")

    # ---- 6. старое сообщение: повторный клик той же кнопкой не запускает вторую игру
    ic = FInter(A, ch, msg); await btn.callback(ic); assert ic.modal is None
    assert cash(bot, A) == c0 - 50
    # доиграть новый раунд
    await shoot(new_game, A, ch); await cashout(new_game, A, ch)
    assert new_game.ended and money(bot) == m0 and not bot.active_games.docs and not cog.active_rr
    # и после этого старая кнопка первого раунда остаётся одноразовой
    ic = FInter(A, ch, msg); await btn.callback(ic); assert ic.modal is None and "уже" in ic.replies[-1]
    print("T6 старая кнопка одноразовая; новый раунд доигран, деньги сходятся")

    # ---- 7. недостаточный баланс
    broke = 24; await bot.ensure_user(G, broke)
    force_rng(False); msg2, g2 = await start_game(cog, ch, G, [(Bb, 100)])
    await shoot(g2, Bb, ch); await cashout(g2, Bb, ch)
    # Bb тратит всё, что есть, на другой счёт
    bb_cash = cash(bot, Bb)
    await bot.change_cash(G, Bb, -bb_cash); await bot.change_reserve(G, bb_cash)
    i = FInter(Bb, ch, msg2); await g2.final_view.btn_again.callback(i)
    assert i.modal is None and "Недостаточно" in i.replies[-1]
    await give(bot, Bb, 5)            # меньше минимальной ставки (10) — тоже отказ
    i = FInter(Bb, ch, msg2); await g2.final_view.btn_again.callback(i)
    assert i.modal is None
    await give(bot, Bb, 5000)
    i = FInter(Bb, ch, msg2); await g2.final_view.btn_again.callback(i); assert i.modal is not None
    # баланс упал между нажатием и отправкой формы
    stolen = cash(bot, Bb) - 5
    await bot.change_cash(G, Bb, -stolen); await bot.change_reserve(G, stolen)
    i.modal.bet_input.value = "100"; i3 = FInter(Bb, ch, msg2); await i.modal.on_submit(i3)
    assert i3.sent_msg is None and i3.replies and (G, Bb) not in cog.active_rr
    print("T7 проверка баланса перед запуском нового раунда ok")

    # ---- 8. мультиплеер: оба участника жмут «Играть снова», параллельно, каждый — свой раунд
    await give(bot, A, 5000); await give(bot, Bb, 5000)
    force_rng(False)
    mm, mg = await start_game(cog, ch, G, [(A, 100), (Bb, 100)])
    # A стреляет и забирает; Bb стреляет и погибает? -> Bb ходит вторым: форсируем гибель
    await shoot(mg, A, ch); await cashout(mg, A, ch)
    force_rng(die=True); await shoot(mg, Bb, ch)
    assert mg.ended, "в мультиплеере последний выживший побеждает автоматически"
    av = mg.final_view
    async def again_flow(uid, bet):
        i = FInter(uid, ch, mm); await av.btn_again.callback(i)
        i.modal.bet_input.value = str(bet); s = FInter(uid, ch, mm); await i.modal.on_submit(s)
        return s
    before = {A: cash(bot, A), Bb: cash(bot, Bb)}
    sa, sb = await asyncio.gather(again_flow(A, 40), again_flow(Bb, 60))
    assert isinstance(sa.sent_msg.view, B.RRModeView) and isinstance(sb.sent_msg.view, B.RRModeView)
    assert sa.sent_msg.view.bet == 40 and sb.sent_msg.view.bet == 60 and sa.sent_msg is not sb.sent_msg
    assert cash(bot, A) == before[A] and cash(bot, Bb) == before[Bb]
    force_rng(False)
    ra, rb = FInter(A, ch, sa.sent_msg), FInter(Bb, ch, sb.sent_msg)
    await asyncio.gather(sa.sent_msg.view.btn_solo.callback(ra), sb.sent_msg.view.btn_solo.callback(rb))
    assert cash(bot, A) == before[A] - 40 and cash(bot, Bb) == before[Bb] - 60
    ga, gb = sa.sent_msg.view, sb.sent_msg.view
    assert ga.players[0].uid == A and gb.players[0].uid == Bb and ga is not gb
    # Bb пытается нажать кнопку на игре A — нельзя
    i = await shoot(ga, Bb, ch); assert ga.players[0].steps == 0
    for g_, u in ((ga, A), (gb, Bb)):
        await shoot(g_, u, ch); await cashout(g_, u, ch)
    assert not bot.active_games.docs and not cog.active_rr
    print("T8 мультиплеер: два игрока жмут одновременно, каждый получает свой раунд")

    # ---- 9. проигрыш в соло -> кнопка тоже есть, новый раунд работает; сохранение выплат/множителей
    force_rng(die=True)
    ml, lg = await start_game(cog, ch, G, [(A, 100)])
    c0 = cash(bot, A); await shoot(lg, A, ch)
    assert lg.ended and isinstance(ml.view, B.RRAgainView) and cash(bot, A) == c0
    assert B.rr_multiplier(1) == 1.18 and B.rr_multiplier(2) == 1.41 and B.rr_multiplier(3) == 1.69
    print("T9 после проигрыша кнопка доступна; множители не изменились")

    # ---- 10. изоляция серверов: кнопка с другого сервера отклоняется
    i = FInter(A, ch, ml, gid=2); ok = await ml.view.interaction_check(i)
    assert ok is False and cash(bot, A) == c0
    # казино выключено -> кнопка не работает
    await bot.settings.update_one({"_id": G}, {"$set": {"modules": {"casino": False}}}, upsert=True)
    bot.invalidate_settings(G)
    i = FInter(A, ch, ml); await ml.view.btn_again.callback(i); assert i.modal is None
    print("T10 изоляция серверов и отключённый модуль ok")
    print("ВСЕ ТЕСТЫ РУЛЕТКИ ПРОШЛИ")
asyncio.run(main())
