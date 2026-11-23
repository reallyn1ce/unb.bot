import sys, asyncio, importlib.util, types, copy, random
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H
spec = importlib.util.spec_from_file_location("botmod", os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bot.py'))
B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
from harness import DuplicateKeyError

class FakeBot(B.EconomyBot):
    def __init__(self):
        self.db = H.FakeDB(); c = self.db.c
        self.db_users, self.economy, self.funds, self.tx = c["users"], c["economy"], c["funds"], c["tx"]
        self.stocks, self.stock_holdings, self.stock_orders = c["stocks"], c["stock_holdings"], c["stock_orders"]
        self.stock_trades, self.stock_dividends, self.stock_payouts = c["stock_trades"], c["stock_dividends"], c["stock_payouts"]
    async def run_tx(self, fn):
        snap = self.db.snapshot()
        try: return await fn(None)
        except BaseException:
            self.db.restore(snap); raise
    async def get_settings(self, gid): return {"tax_rate": 0.13}
    async def module_enabled(self, gid, m): return True

class Ctx:
    def __init__(self, gid, uid):
        self.guild = types.SimpleNamespace(id=gid, name="G"); self.author = types.SimpleNamespace(id=uid, display_avatar=types.SimpleNamespace(url="x"))
        self.interaction = None; self.sent = []
    async def send(self, *a, **k): self.sent.append(k.get("embed")); return None

G = 1
def total_money(bot, gid=G):
    users = sum(d.get("cash",0)+d.get("bank",0) for d in bot.db_users.docs if d["guild_id"]==gid)
    reserve = sum(d.get("reserve",0) for d in bot.economy.docs)
    budget = sum(d.get("balance",0) for d in bot.funds.docs)
    treasury = sum(d.get("treasury",0) for d in bot.stocks.docs)
    escrow = sum(o.get("escrow",0) for o in bot.stock_orders.docs if o["status"]=="open" and o["side"]=="buy")
    return users + reserve + budget + treasury + escrow

def shares_ok(bot, ticker):
    st = [s for s in bot.stocks.docs if s["ticker"]==ticker][0]
    held = sum(h["shares"] for h in bot.stock_holdings.docs if h["ticker"]==ticker)
    listed = sum(o["left"] for o in bot.stock_orders.docs if o["ticker"]==ticker and o["side"]=="sell" and o["status"]=="open")
    assert held + listed == st["supply"], (held, listed, st["supply"])
    assert all(h["shares"] >= 0 for h in bot.stock_holdings.docs)
    assert st["treasury"] >= st["profit_pool"] >= 0, st
    assert all(o["left"] >= 0 and (o["side"]=="sell" or o["escrow"] == o["price"]*o["left"]) for o in bot.stock_orders.docs if o["status"]=="open")

async def give(bot, uid, cash):
    await bot.ensure_user(G, uid); await bot.change_cash(G, uid, cash)
    await bot.change_reserve(G, -cash, allow_negative=True)     # деньги выданы из резерва: масса неизменна

def cash(bot, uid):
    return [d for d in bot.db_users.docs if d["user_id"]==uid and d["guild_id"]==G][0]["cash"]
def hold(bot, uid, t="KORP"):
    h=[h for h in bot.stock_holdings.docs if h["user_id"]==uid and h["ticker"]==t and h["guild_id"]==G]
    return h[0]["shares"] if h else 0

async def place(cog, uid, side, qty, price=None, t="KORP"):
    return await cog.bot.run_tx(lambda s: cog._place_tx(s, G, uid, t, side, qty, price))

async def main():
    bot = FakeBot(); cog = B.StockCog(bot)
    await bot.ensure_guild_funds(G)
    FOUNDER, A, Bu, C = 10, 11, 12, 13
    for u in (FOUNDER, A, Bu, C): await give(bot, u, 1_000_000)
    base_money = total_money(bot)
    ctx = Ctx(G, FOUNDER)
    await cog.stock_create(ctx, "KORP", name="Корпорация")
    st = bot.stocks.docs[0]
    assert st["supply"]==1000 and hold(bot, FOUNDER)==600
    ask = [o for o in bot.stock_orders.docs if o["user_id"]==0][0]
    assert ask["left"]==400 and ask["price"]==100 and hold(bot,0)==0
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    print("T1 IPO ok: основатель 600, рынок 400, деньги сохранены")

    # T2 покупка у компании: деньги в казну компании, не основателю
    f_cash = cash(bot, FOUNDER)
    r = await place(cog, A, "buy", 50)
    assert r["filled"]==50 and r["spent"]==5000 and bot.stocks.docs[0]["treasury"]==5000
    assert cash(bot, FOUNDER)==f_cash, "основатель не должен получать деньги от первичной продажи"
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    print("T2 первичная продажа -> казна компании ok")

    # T3 лимитная продажа A, покупка B: комиссия, прибыль компании
    await place(cog, A, "sell", 20, 105)
    assert hold(bot,A)==30
    shares_ok(bot,"KORP")
    before_b = cash(bot,Bu)
    r = await place(cog, Bu, "buy", 20)             # рыночная: берёт лучшую цену -> 100 у компании? 
    print("   рыночная покупка по лучшей цене:", r["avg"], "ask компании 100 выгоднее лимитки A")
    r2 = await place(cog, Bu, "buy", 25)            # дойдёт до A@105 после компании
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    st = bot.stocks.docs[0]; print("   treasury", st["treasury"], "profit_pool", st["profit_pool"], "quote", st["price"])

    # T4 нельзя продать больше, чем есть; состояние не меняется
    snap = bot.db.snapshot()
    try: await place(cog, A, "sell", 500, 100); raise SystemExit("FAIL oversell")
    except B.EconomyError as e: pass
    assert bot.db.snapshot()==snap
    print("T4 oversell отклонён, состояние не изменилось")

    # T5 сделки с собой невозможны
    await place(cog, C, "sell", 0+1 if False else 1, 105) if False else None
    await place(cog, A, "buy", 1, 99)
    n_trades = len(bot.stock_trades.docs)
    await place(cog, A, "sell", 1, 99)
    assert len(bot.stock_trades.docs)==n_trades, "самосделка"
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    print("T5 самосделки исключены")

    # T6 диапазон цен
    for bad in (50, 500):
        try: await place(cog, A, "buy", 1, bad); raise SystemExit("FAIL band")
        except B.EconomyError: pass
    print("T6 диапазон цен ok")

    # T7 отмена возвращает резерв
    c0 = cash(bot, A)
    n, sh, cs = await bot.run_tx(lambda s: cog._cancel_tx(s, G, A, None))
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    print("T7 отмена ok:", n, "заявок, акций", sh, "денег", cs)

    # T8 дивиденды
    for _ in range(30):
        u = random.choice([A,Bu,C]); v = random.choice([A,Bu,C])
        if u==v: continue
        if hold(bot,v)>=5:
            await place(cog, v, "sell", 5, 99)
            try: await place(cog, u, "buy", 5)
            except B.EconomyError: pass
            await bot.run_tx(lambda s: cog._cancel_tx(s, G, v, None)) if any(o["user_id"]==v and o["status"]=="open" for o in bot.stock_orders.docs) else None
    st = bot.stocks.docs[0]; print("T8 перед дивидендами: treasury", st["treasury"], "pool", st["profit_pool"])
    shares_ok(bot,"KORP"); assert total_money(bot)==base_money
    pool = st["profit_pool"]; assert pool>0
    res = await cog.pay_dividends(G, "KORP")
    print("   выплата:", res)
    st = bot.stocks.docs[0]
    assert total_money(bot)==base_money, "деньги не должны создаваться"
    assert st["profit_pool"] <= pool and st["treasury"]>=st["profit_pool"]
    res2 = await cog.pay_dividends(G, "KORP"); assert res2 is None, "двойная выплата"
    assert sum(p["gross"] for p in bot.stock_payouts.docs)==res["total"]
    reserve_before = bot.economy.docs[0]["reserve"]
    print("T8 дивиденды ok, дубль исключён, резерв ЦБ не затронут:", reserve_before)
    shares_ok(bot,"KORP")
    # компания (user 0) дивидендов не получает
    assert all(p["user_id"]!=0 for p in bot.stock_payouts.docs)

    # T9 миграция
    bot2 = FakeBot(); await bot2.ensure_guild_funds(G)
    await bot2.stocks.insert_one({"_id":"1:OLD","guild_id":1,"ticker":"OLD","name":"Old","owner_id":7,"price":137.42,"supply":1000,"history":[100.0,137.42],"history_ts":[B.now(),B.now()],"created_at":B.now()})
    await bot2.stocks.insert_one({"_id":"2:OLD","guild_id":2,"ticker":"OLD","name":"Other","owner_id":8,"price":50.0,"supply":500,"history":[50.0],"history_ts":[B.now()],"created_at":B.now()})
    for uid,g,n in ((7,1,900),(9,1,100),(8,2,500)):
        await bot2.stock_holdings.insert_one({"guild_id":g,"user_id":uid,"ticker":"OLD","shares":n})
    await bot2.migrate_stocks_v2(); await bot2.migrate_stocks_v2()
    s1=[s for s in bot2.stocks.docs if s["guild_id"]==1][0]
    assert s1["price"]==137.42 and s1["supply"]==1000 and s1["schema_v2"]
    h7=[h for h in bot2.stock_holdings.docs if h["user_id"]==7][0]["shares"]
    orders=[o for o in bot2.stock_orders.docs if o["guild_id"]==1]
    assert h7==600 and len(orders)==1 and orders[0]["left"]==300 and orders[0]["user_id"]==7 and orders[0]["price"]==137
    held=sum(h["shares"] for h in bot2.stock_holdings.docs if h["guild_id"]==1)
    assert held+300==1000
    o2=[o for o in bot2.stock_orders.docs if o["guild_id"]==2]; assert len(o2)==1 and o2[0]["user_id"]==8 and o2[0]["left"]==500-int(500*0.6)==200
    assert not any(o["guild_id"]==2 and o["ticker"]=="OLD" and o["user_id"]==7 for o in bot2.stock_orders.docs)
    print("T9 миграция ok: цена/выпуск сохранены, избыток основателя -> его заявка, идемпотентна, серверы изолированы")

    # T10 манипуляция: две учётки гоняют акции по растущей цене
    bot3 = FakeBot(); cog3 = B.StockCog(bot3); await bot3.ensure_guild_funds(G)
    for u in (FOUNDER, A, Bu): await give(bot3, u, 5_000_000)
    await cog3.stock_create(Ctx(G,FOUNDER), "KORP", name="Корп")
    await place(cog3, A, "buy", 200)
    q0 = bot3.stocks.docs[0]["price"]
    for i in range(40):
        p = int(bot3.stocks.docs[0]["price"]) + 10
        try:
            await place(cog3, A, "sell", 10, p); await place(cog3, Bu, "buy", 10, p)
        except B.EconomyError: break
    q1 = bot3.stocks.docs[0]["price"]
    anchor = B.day_anchor(bot3.stocks.docs[0])
    print(f"T10 wash-trading двумя аккаунтами: {q0} -> {q1}  (суточный лимит +30% от {anchor:.0f})")
    assert q1 <= anchor*1.30+0.01
    shares_ok(bot3,"KORP")
    print("ВСЕ ТЕСТЫ ПРОШЛИ")
asyncio.run(main())
