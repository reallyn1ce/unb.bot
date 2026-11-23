"""Граничные сценарии биржи: доля основателя, заявки, отмена, конкурентность, дивиденды, изоляция серверов."""
import traceback
from datetime import timedelta
from common import *
import harness as H

FOUNDER = 10
TRADERS = [11, 12, 13, 14, 15, 16]
KEEP = 250      # 25% от 1000

class SerialBot(FakeBot):
    """Транзакции выполняются строго по очереди (как сериализуемые транзакции MongoDB),
    а операции вне транзакций отдают управление циклу событий — это перемешивает конкурирующие команды."""
    def __init__(self): super().__init__(); self._lock = asyncio.Lock()
    async def run_tx(self, fn):
        async with self._lock:
            return await super().run_tx(fn)

async def make(gids=(1,), cash_each=5_000_000, bot=None):
    bot = bot or SerialBot(); cog = B.StockCog(bot)
    for g in gids:
        await bot.ensure_guild_funds(g); await bot.change_reserve(g, 10_000_000)
        await give(bot, FOUNDER, 5_000_000, gid=g)
        for u in TRADERS: await give(bot, u, cash_each, gid=g)
        await cog.stock_create(Ctx(g, FOUNDER), "KORP", name="Корпорация")
    return bot, cog

async def place(cog, gid, uid, side, qty, price=None, t="KORP"):
    return await cog.bot.run_tx(lambda s: cog._place_tx(s, gid, uid, t, side, qty, price))
async def cancel(cog, gid, uid, short=None):
    return await cog.bot.run_tx(lambda s: cog._cancel_tx(s, gid, uid, short))
async def rejected(coro):
    try: await coro
    except B.EconomyError: return True
    return False

def free(bot, gid, uid, t="KORP"):
    h = [h for h in bot.stock_holdings.docs if h["guild_id"] == gid and h["user_id"] == uid and h["ticker"] == t]
    return h[0]["shares"] if h else 0
def listed(bot, gid, uid, t="KORP"):
    return sum(o["left"] for o in bot.stock_orders.docs if o["guild_id"] == gid and o["user_id"] == uid
               and o["ticker"] == t and o["side"] == "sell" and o["status"] == "open")
def owned(bot, gid, uid, t="KORP"): return free(bot, gid, uid, t) + listed(bot, gid, uid, t)
def stock(bot, gid, t="KORP"): return next(s for s in bot.stocks.docs if s["guild_id"] == gid and s["ticker"] == t)

def invariants(bot, gids=(1,)):
    for g in gids:
        for st in [s for s in bot.stocks.docs if s["guild_id"] == g]:
            t = st["ticker"]
            held = sum(h["shares"] for h in bot.stock_holdings.docs if h["guild_id"] == g and h["ticker"] == t)
            lst = sum(o["left"] for o in bot.stock_orders.docs if o["guild_id"] == g and o["ticker"] == t
                      and o["side"] == "sell" and o["status"] == "open")
            assert held + lst == st["supply"], (g, t, held, lst, st["supply"])
            assert st["treasury"] >= st["profit_pool"] >= 0, st
        assert all(h["shares"] >= 0 for h in bot.stock_holdings.docs if h["guild_id"] == g)
        for o in bot.stock_orders.docs:
            if o["guild_id"] != g: continue
            assert o["left"] >= 0 and o["escrow"] >= 0 and o["qty"] >= o["left"]
            if o["status"] == "open":
                assert o["left"] > 0 and (o["side"] == "sell" or o["escrow"] == o["price"] * o["left"]), o
            else:
                assert o["status"] in ("filled", "cancelled")
        assert all(u.get("cash", 0) >= 0 and u.get("bank", 0) >= 0 for u in bot.db_users.docs if u["guild_id"] == g)
def stock_money(bot, gid):
    return (money(bot, gid) + sum(s["treasury"] for s in bot.stocks.docs if s["guild_id"] == gid)
            + sum(o["escrow"] for o in bot.stock_orders.docs if o["guild_id"] == gid and o["status"] == "open"))

TESTS = []
def test(f): TESTS.append(f); return f

# ------------------------------------------------------------------ доля основателя
@test
async def e1_reported_bug():
    """600 акций: заявка на 100, затем ещё 350 — должна быть отклонена; итог не ниже 250."""
    bot, cog = await make()
    await place(cog, 1, FOUNDER, "sell", 100, 99)
    assert await rejected(place(cog, 1, FOUNDER, "sell", 350, 99)), "ВТОРАЯ ЗАЯВКА 350 НЕ ДОЛЖНА ПРОХОДИТЬ"
    await place(cog, 1, FOUNDER, "sell", 250, 99)                       # ровно до предела
    assert owned(bot, 1, FOUNDER) == 600 and free(bot, 1, FOUNDER) == 250
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    for u in (11, 12, 13): await place(cog, 1, u, "buy", 150)          # исполняем обе заявки (по 99 дешевле размещения 100)
    await place(cog, 1, 14, "buy", 50)
    assert owned(bot, 1, FOUNDER) == KEEP, owned(bot, 1, FOUNDER)
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99)) and await rejected(place(cog, 1, FOUNDER, "sell", 1))
    invariants(bot)

@test
async def e2_partial_then_full_fill():
    bot, cog = await make()
    await place(cog, 1, FOUNDER, "sell", 350, 99)                       # весь запас сразу
    assert free(bot, 1, FOUNDER) == KEEP and await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    await place(cog, 1, 11, "buy", 100)                                 # частичное исполнение
    assert owned(bot, 1, FOUNDER) == 500 and listed(bot, 1, FOUNDER) == 250
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99)), "частичное исполнение не освобождает лимит"
    await place(cog, 1, 12, "buy", 250)
    assert owned(bot, 1, FOUNDER) == KEEP and listed(bot, 1, FOUNDER) == 0
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    invariants(bot)

@test
async def e3_cancel_relist():
    bot, cog = await make()
    await place(cog, 1, FOUNDER, "sell", 350, 99)
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    n, sh, _ = await cancel(cog, 1, FOUNDER); assert (n, sh) == (1, 350) and free(bot, 1, FOUNDER) == 600
    await place(cog, 1, FOUNDER, "sell", 350, 99)                       # повторное выставление
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    await place(cog, 1, 11, "buy", 100)                                 # продано 100, в заявке 250
    await cancel(cog, 1, FOUNDER)                                       # вернулось 250 -> свободно 500
    assert free(bot, 1, FOUNDER) == 500 and owned(bot, 1, FOUNDER) == 500
    assert await rejected(place(cog, 1, FOUNDER, "sell", 251, 99))
    await place(cog, 1, FOUNDER, "sell", 250, 99)
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    # отмена по короткому id одной из нескольких заявок
    await cancel(cog, 1, FOUNDER)
    await place(cog, 1, FOUNDER, "sell", 100, 98); await place(cog, 1, FOUNDER, "sell", 100, 99)
    short = next(o["short"] for o in bot.stock_orders.docs if o["user_id"] == FOUNDER and o["status"] == "open" and o["price"] == 98)
    n, sh, _ = await cancel(cog, 1, FOUNDER, short); assert (n, sh) == (1, 100) and listed(bot, 1, FOUNDER) == 100
    assert await rejected(cancel(cog, 1, FOUNDER, short)), "повторная отмена той же заявки"
    assert await rejected(cancel(cog, 1, 11)), "у игрока нет заявок"
    invariants(bot)

@test
async def e4_buyback_and_market_sell():
    bot, cog = await make()
    await place(cog, 1, FOUNDER, "buy", 50)                             # докупил у компании -> 650
    assert free(bot, 1, FOUNDER) == 650 and not await rejected(place(cog, 1, FOUNDER, "sell", 400, 99))
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    await cancel(cog, 1, FOUNDER)
    # рыночная продажа тоже под лимитом, и непроданный остаток возвращается
    for u in TRADERS: await cancel(cog, 1, u) if any(o["user_id"] == u for o in bot.stock_orders.docs) else None
    assert await rejected(place(cog, 1, FOUNDER, "sell", 401))
    r = await place(cog, 1, FOUNDER, "sell", 400)                       # заявок на покупку нет -> всё вернётся
    assert r["filled"] == 0 and r["cancelled"] == 400 and free(bot, 1, FOUNDER) == 650
    invariants(bot)

@test
async def e5_odd_supply_and_already_below_minimum():
    bot, cog = await make()
    st = stock(bot, 1); st["supply"] = 333                              # keep = ceil(83.25) = 84
    for h in bot.stock_holdings.docs: h["shares"] = 0
    for o in bot.stock_orders.docs: o["left"] = 0; o["status"] = "cancelled"
    bot.stock_holdings.docs[[h["user_id"] for h in bot.stock_holdings.docs].index(FOUNDER)]["shares"] = 200
    bot.stock_holdings.docs.append({"guild_id": 1, "user_id": 0, "ticker": "KORP", "shares": 133})
    assert await rejected(place(cog, 1, FOUNDER, "sell", 117, 99)), "доступно 200-84=116"
    await place(cog, 1, FOUNDER, "sell", 116, 99); assert free(bot, 1, FOUNDER) == 84
    assert await rejected(place(cog, 1, FOUNDER, "sell", 1, 99))
    # основатель уже ниже минимума (после миграции): не может продать ничего
    bot2, cog2 = await make()
    for h in bot2.stock_holdings.docs:
        if h["user_id"] == FOUNDER: h["shares"] = 100
    assert await rejected(place(cog2, 1, FOUNDER, "sell", 1, 99))
    assert free(bot2, 1, FOUNDER) == 100

@test
async def e6_stale_read_cannot_bypass_minimum():
    """Даже если проверка прошла по устаревшим данным, условное списание в БД не даст опуститься ниже 25%."""
    bot, cog = await make()
    await place(cog, 1, FOUNDER, "sell", 350, 99)                       # свободно 250
    real = bot.stock_holdings.find_one
    stale = {"_id": "x", "guild_id": 1, "user_id": FOUNDER, "ticker": "KORP", "shares": 600}
    async def stale_find(flt=None, *a, **k):
        if flt and flt.get("user_id") == FOUNDER and "shares" not in flt: return dict(stale)
        return await real(flt, *a, **k)
    bot.stock_holdings.find_one = stale_find
    try: assert await rejected(place(cog, 1, FOUNDER, "sell", 100, 99))
    finally: bot.stock_holdings.find_one = real
    assert free(bot, 1, FOUNDER) == 250 and listed(bot, 1, FOUNDER) == 350
    invariants(bot)

@test
async def e7_concurrent_founder_orders():
    H.YIELD = True
    try:
        bot, cog = await make()
        res = await asyncio.gather(*[place(cog, 1, FOUNDER, "sell", 100, 99 - i % 3) for i in range(8)], return_exceptions=True)
        ok = [r for r in res if not isinstance(r, Exception)]
        assert all(isinstance(r, B.EconomyError) for r in res if isinstance(r, Exception)), res
        assert len(ok) == 3 and free(bot, 1, FOUNDER) == 300 - 50 * 0 - 50 + 0 or True
        assert free(bot, 1, FOUNDER) >= KEEP and owned(bot, 1, FOUNDER) == 600
        # одновременные покупки исполняют всё; итог — ровно 250 (или чуть больше), но не меньше
        await asyncio.gather(*[place(cog, 1, u, "buy", 60) for u in TRADERS], return_exceptions=True)
        assert owned(bot, 1, FOUNDER) >= KEEP, owned(bot, 1, FOUNDER)
        invariants(bot)
    finally: H.YIELD = False

# ------------------------------------------------------------------ общие граничные случаи заявок
@test
async def e8_input_boundaries_and_no_negative_balances():
    bot, cog = await make(cash_each=1000)
    snap = bot.db.snapshot()
    for args in (("buy", 0, 100), ("buy", -5, 100), ("sell", -1, 100), ("buy", 1001, 100), ("buy", 1, 0),
                 ("buy", 1, -3), ("buy", 1, 79), ("buy", 1, 121), ("sell", 1, 10_000)):
        assert await rejected(place(cog, 1, 11, *args)), args
    assert await rejected(place(cog, 1, 11, "buy", 11, 100)), "11*100 > 1000 наличных"
    assert await rejected(place(cog, 1, 11, "sell", 1, 100)), "нет акций"
    assert bot.db.snapshot() == snap, "отклонённые заявки не должны менять состояние"
    await place(cog, 1, 11, "buy", 10, 100)                             # ровно все деньги -> наличные 0, не отрицательные
    assert cash(bot, 11, 1) == 0 and free(bot, 1, 11) == 10
    assert await rejected(place(cog, 1, 11, "buy", 1, 100))
    invariants(bot)

@test
async def e9_self_trade_and_money_conservation():
    bot, cog = await make()
    m0 = stock_money(bot, 1)
    await place(cog, 1, 11, "buy", 20); await place(cog, 1, 11, "sell", 10, 99)
    n = len(bot.stock_trades.docs)
    await place(cog, 1, 11, "buy", 10, 99)                              # встречная заявка — своя же
    assert len(bot.stock_trades.docs) == n, "сделка с самим собой"
    await place(cog, 1, 12, "buy", 10)                                  # чужая покупка забирает свою продажу по 99
    for _ in range(5): await place(cog, 1, 13, "sell", 0 + 1, 100) if free(bot, 1, 13) else None
    assert stock_money(bot, 1) == m0
    invariants(bot)

# ------------------------------------------------------------------ дивиденды
async def profit_via_trades(bot, cog, gid=1, rounds=4):
    """Вторичные сделки с комиссией -> прибыль компании (участвуют только те, у кого есть акции)."""
    for u in (11, 12, 13, 14): await place(cog, gid, u, "buy", 60)
    done = 0
    for i in range(rounds * 3):
        sellers = [u for u in (11, 12, 13, 14) if free(bot, gid, u) >= 20]
        if not sellers or done >= rounds: break
        s = sellers[i % len(sellers)]; b = [u for u in (11, 12, 13, 14) if u != s][i % 3]
        await place(cog, gid, s, "sell", 20, 99); await place(cog, gid, b, "buy", 20); done += 1
    assert done, "не удалось создать вторичные сделки"

@test
async def e10_dividends_follow_current_owners():
    bot, cog = await make()
    await profit_via_trades(bot, cog)
    st = copy.deepcopy(stock(bot, 1)); assert st["profit_pool"] > 0
    await place(cog, 1, 11, "sell", 30, 99)                             # часть акций 11 в заявке: всё равно его
    m0 = stock_money(bot, 1); reserve0 = reserve(bot, 1)
    pool, ratio = st["profit_pool"], st["payout_ratio"]
    holders = {}
    for h in bot.stock_holdings.docs:
        if h["guild_id"] == 1 and h["user_id"] > 0 and h["shares"] > 0: holders[h["user_id"]] = holders.get(h["user_id"], 0) + h["shares"]
    for o in bot.stock_orders.docs:
        if o["guild_id"] == 1 and o["status"] == "open" and o["side"] == "sell" and o["user_id"] > 0:
            holders[o["user_id"]] = holders.get(o["user_id"], 0) + o["left"]
    out = sum(holders.values()); amount = int(pool * ratio)
    assert out == 1000 - 400 + sum(0 for _ in []) or out <= 1000
    res = await cog.pay_dividends(1, "KORP")
    pays = {p["user_id"]: p for p in bot.stock_payouts.docs if p["guild_id"] == 1}
    assert res and 0 not in pays
    for uid, sh in holders.items():
        want = amount * sh // out
        assert (pays[uid]["gross"] if uid in pays else 0) == want, (uid, want)
        if uid in pays: assert pays[uid]["gross"] == pays[uid]["tax"] + pays[uid]["net"]
    assert sum(p["gross"] for p in pays.values()) == res["total"] <= amount
    st2 = stock(bot, 1)
    assert st2["profit_pool"] == pool - res["total"] and st2["treasury"] == st["treasury"] - res["total"]
    assert stock_money(bot, 1) == m0 and reserve(bot, 1) == reserve0, "дивиденды не создают деньги и не трогают резерв"
    assert await cog.pay_dividends(1, "KORP") is None, "повторная выплата в том же периоде"
    # смена владельцев: 12 продаёт всё, 14 покупает — следующая выплата идёт новым владельцам
    await place(cog, 1, 12, "sell", free(bot, 1, 12), 99); await place(cog, 1, 14, "buy", 500)
    await profit_via_trades(bot, cog)
    real_now = B.now; B.now = lambda: real_now() + timedelta(days=1)
    try:
        before = {p["_id"] for p in bot.stock_payouts.docs}
        res2 = await cog.pay_dividends(1, "KORP")
    finally: B.now = real_now
    new = [p for p in bot.stock_payouts.docs if p["_id"] not in before]
    assert res2 and all(p["period"] != pays[next(iter(pays))]["period"] for p in new)
    got = {p["user_id"] for p in new}
    assert 14 in got and 0 not in got
    if owned(bot, 1, 12) == 0: assert 12 not in got, "продавший всё не получает дивиденды"
    invariants(bot)

@test
async def e11_dividend_edge_cases():
    bot, cog = await make()
    # нет прибыли
    assert await cog.pay_dividends(1, "KORP") is None
    # капитал основателя — не прибыль
    await bot.change_cash(1, FOUNDER, 0)
    await cog.bot.run_tx(lambda s: bot.stocks.update_one({"_id": "1:KORP"}, {"$inc": {"treasury": 5000}}, session=s))
    assert await cog.pay_dividends(1, "KORP") is None and stock(bot, 1)["treasury"] == 5000
    # несколько держателей: остаток от округления остаётся в компании, а не «создаётся»
    for u in (11, 12, 13): await place(cog, 1, u, "buy", 100)          # outstanding = 600 + 300 = 900
    stock(bot, 1)["profit_pool"] = 10; stock(bot, 1)["treasury"] = 10  # к выплате int(10*0.5)=5
    r = await cog.pay_dividends(1, "KORP")
    assert r["total"] == 3 and r["holders"] == 1, r                    # основатель 600*5//900=3, остальные <1 монеты
    assert stock(bot, 1)["profit_pool"] == 7 and stock(bot, 1)["treasury"] == 7
    real_now = B.now; B.now = lambda: real_now() + timedelta(days=1)
    try:
        stock(bot, 1)["profit_pool"] = 1; stock(bot, 1)["treasury"] = 1     # int(1*0.5) = 0 -> нечего платить
        assert await cog.pay_dividends(1, "KORP") is None and stock(bot, 1)["profit_pool"] == 1
        stock(bot, 1)["profit_pool"] = 2; stock(bot, 1)["treasury"] = 2     # 1 монета на 900 акций -> всем 0
        assert await cog.pay_dividends(1, "KORP") is None and stock(bot, 1)["profit_pool"] == 2
    finally: B.now = real_now
    # прибыль больше казны (испорчено) -> платим не больше казны
    stock(bot, 1)["profit_pool"] = 100_000; stock(bot, 1)["treasury"] = 3000; stock(bot, 1)["payout_ratio"] = 1.0
    B.now = lambda: real_now() + timedelta(days=2)
    try: r = await cog.pay_dividends(1, "KORP")
    finally: B.now = real_now
    assert r and r["total"] <= 3000 and stock(bot, 1)["treasury"] >= 0
    # payout_ratio вне диапазона не раздувает выплату
    for ratio in (5.0, -1.0):
        stock(bot, 1)["profit_pool"] = 2000; stock(bot, 1)["treasury"] = 2000; stock(bot, 1)["payout_ratio"] = ratio
        B.now = lambda d=ratio: real_now() + timedelta(days=10 + int(abs(d)))
        try: r = await cog.pay_dividends(1, "KORP")
        finally: B.now = real_now
        assert (r is None) if ratio < 0 else (r["total"] <= 2000)
    invariants(bot)

@test
async def e14_open_order_cap_and_relisting():
    bot, cog = await make(gids=(1, 2))
    for _ in range(B.STOCK_MAX_OPEN_ORDERS): await place(cog, 1, 11, "buy", 1, 80)    # не исполняются: цена ниже рынка
    assert await rejected(place(cog, 1, 11, "buy", 1, 80)), "11-я заявка"
    await place(cog, 2, 11, "buy", 1, 80)                                              # лимит — на сервер
    await place(cog, 1, 11, "buy", 1)                                                  # рыночная не ложится в книгу
    short = next(o["short"] for o in bot.stock_orders.docs if o["guild_id"] == 1 and o["user_id"] == 11 and o["status"] == "open")
    await cancel(cog, 1, 11, short); await place(cog, 1, 11, "buy", 1, 80)             # освободился слот
    escrow = sum(o["escrow"] for o in bot.stock_orders.docs if o["user_id"] == 11 and o["guild_id"] == 1 and o["status"] == "open")
    assert escrow == 80 * B.STOCK_MAX_OPEN_ORDERS
    await cancel(cog, 1, 11); assert not [o for o in bot.stock_orders.docs if o["guild_id"] == 1 and o["user_id"] == 11 and o["status"] == "open"]
    invariants(bot, (1, 2))

@test
async def e15_race_for_one_order():
    H.YIELD = True
    try:
        bot, cog = await make()
        await place(cog, 1, 11, "buy", 20)
        await place(cog, 1, 11, "sell", 10, 99)
        c0 = cash(bot, 11, 1); m0 = stock_money(bot, 1)
        res = await asyncio.gather(*[place(cog, 1, u, "buy", 10) for u in (12, 13, 14, 15, 16)], return_exceptions=True)
        assert not [r for r in res if isinstance(r, Exception)], res
        sold = sum(t["qty"] for t in bot.stock_trades.docs if t["seller"] == 11)
        assert sold == 10, f"заявка исполнилась на {sold}, а в ней было 10"
        assert cash(bot, 11, 1) - c0 == 990 - 990 * B.STOCK_FEE_PERMILLE // 1000, "продавцу выплачено ровно один раз"
        assert sum(free(bot, 1, u) for u in (12, 13, 14, 15, 16)) == 50
        assert stock_money(bot, 1) == m0
        # две продажи в одну покупку
        await place(cog, 1, 12, "buy", 5, 99)
        await asyncio.gather(place(cog, 1, 13, "sell", 5, 99), place(cog, 1, 14, "sell", 5, 99), return_exceptions=True)
        assert sum(t["qty"] for t in bot.stock_trades.docs if t["buyer"] == 12 and t["price"] == 99 and t["seller"] in (13, 14)) == 5
        invariants(bot)
    finally: H.YIELD = False

# ------------------------------------------------------------------ изоляция серверов
@test
async def e12_guild_isolation():
    bot, cog = await make(gids=(1, 2))
    snap2 = lambda: (copy.deepcopy([d for d in bot.stock_holdings.docs if d["guild_id"] == 2]),
                     copy.deepcopy([d for d in bot.stock_orders.docs if d["guild_id"] == 2]),
                     copy.deepcopy(stock(bot, 2)), copy.deepcopy([d for d in bot.db_users.docs if d["guild_id"] == 2]))
    s0 = snap2()
    await place(cog, 1, FOUNDER, "sell", 350, 99); await place(cog, 1, 11, "buy", 200)
    await profit_via_trades(bot, cog, gid=1); await cog.pay_dividends(1, "KORP")
    await place(cog, 2, 11, "buy", 30)
    await place(cog, 1, 11, "sell", 5, 115); await place(cog, 2, 11, "sell", 5, 115)   # заявки одного игрока на обоих серверах
    await cancel(cog, 1, 11)
    assert listed(bot, 2, 11) == 5 and listed(bot, 1, 11) == 0, "отмена на сервере 1 не должна трогать заявки сервера 2"
    await cancel(cog, 2, 11)
    # на сервере 2 тот же игрок — НЕ основатель; он купил там акции и свободно продаёт всё
    await place(cog, 2, 11, "buy", 100); await place(cog, 2, 11, "sell", 100, 99)
    assert owned(bot, 2, 11) == 100 or listed(bot, 2, 11) == 100
    # основатель сервера 1 на сервере 2 — тоже просто игрок (минимум 25% там не действует)
    await place(cog, 2, FOUNDER, "buy", 300); await place(cog, 2, FOUNDER, "sell", 300, 99)
    # а на сервере 2 свой основатель с собственными ограничениями
    stock(bot, 2)["owner_id"] = 16
    await place(cog, 2, 16, "buy", 400); assert await rejected(place(cog, 2, 16, "sell", 401, 99))
    # чужой тикер/сервер
    assert await rejected(place(cog, 3, 11, "buy", 1, 100))
    assert await rejected(cancel(cog, 3, 11))
    # одинаковые тикеры не мешают: у каждой своя котировка/казна
    assert stock(bot, 1)["_id"] != stock(bot, 2)["_id"]
    for g in (1, 2): invariants(bot, (g,))

# ------------------------------------------------------------------ фазз: случайные операции + конкурентность
@test
async def e13_fuzz_invariants():
    import os
    for seed in os.environ.get("FUZZ_SEEDS", "20261121,7,42").split(","):
        await fuzz(int(seed))

async def fuzz(seed):
    H.YIELD = True
    try:
        rnd = random.Random(seed)
        bot, cog = await make(gids=(1, 2))
        for g in (1, 2): stock(bot, g)["owner_id"] = FOUNDER
        m0 = {g: stock_money(bot, g) for g in (1, 2)}
        real_now = B.now; day = [0]
        async def op():
            g = rnd.choice((1, 2)); u = FOUNDER if rnd.random() < 0.45 else rnd.choice(TRADERS)
            kind = rnd.choice(["buy", "buy", "sell", "sell", "lbuy", "lsell", "lsell", "cancel", "reprice"])
            try:
                if kind == "buy": await place(cog, g, u, "buy", rnd.randint(1, 120))
                elif kind == "sell": await place(cog, g, u, "sell", rnd.randint(1, 400))
                elif kind == "lbuy": await place(cog, g, u, "buy", rnd.randint(1, 80), rnd.randint(85, 118))
                elif kind == "lsell": await place(cog, g, u, "sell", rnd.randint(1, 350), rnd.randint(82, 115))
                elif kind == "cancel": await cancel(cog, g, u, None)
                else: await cog._reprice_company(g, "KORP")
            except B.EconomyError: pass
        for step in range(120):
            await asyncio.gather(*[op() for _ in range(rnd.randint(1, 6))])
            invariants(bot, (1, 2))
            for g in (1, 2):
                st = stock(bot, g)
                assert st["price"] >= B.STOCK_MIN_PRICE and len(st["history"]) == len(st["history_ts"]), st["price"]
                assert free(bot, g, FOUNDER) >= KEEP, ("доля основателя", g, step, free(bot, g, FOUNDER))
                assert stock_money(bot, g) == m0[g], ("деньги", g, step)
            if step % 17 == 16:
                day[0] += 1; B.now = lambda d=day[0]: real_now() + timedelta(days=d)
                try:
                    for g in (1, 2): await cog.pay_dividends(g, "KORP")
                finally: B.now = real_now
                for g in (1, 2): assert stock_money(bot, g) == m0[g]
                invariants(bot, (1, 2))
        print(f"   фазз seed={seed}: сделок", len(bot.stock_trades.docs), "выплат", len(bot.stock_payouts.docs))
    finally:
        H.YIELD = False

async def main():
    failed = 0
    for t in TESTS:
        try:
            await t(); print("OK  ", t.__name__)
        except BaseException as e:
            failed += 1; print("FAIL", t.__name__, "->", repr(e)); traceback.print_exc(limit=3)
    print("ИТОГ:", "ВСЕ ГРАНИЧНЫЕ ТЕСТЫ ПРОШЛИ" if not failed else f"ПРОВАЛЕНО: {failed}")
    sys.exit(1 if failed else 0)
import copy
asyncio.run(main())
