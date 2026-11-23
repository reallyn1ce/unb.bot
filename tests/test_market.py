import os, sys
import sys, asyncio
sys.argv=['x']
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_exchange.py')).read().split("async def main():")[0])
async def main():
    bot = FakeBot(); cog = B.StockCog(bot); await bot.ensure_guild_funds(G)
    users=[10,11,12,13,14,15]
    for u in users: await give(bot,u,5_000_000)
    await cog.stock_create(Ctx(G,10),"KORP",name="Корп")
    for u in users[1:]: await place(cog,u,"buy",60)          # раскупили 300 из 400 у компании
    print("после IPO-раскупки: цена", bot.stocks.docs[0]["price"], "казна", bot.stocks.docs[0]["treasury"])
    # реальный спрос: разные пары торгуют по 110
    trades=[(11,12),(13,14),(15,11),(12,13),(14,15),(11,14)]
    for i,(s,b) in enumerate(trades):
        await place(cog,s,"sell",25,110)
        try: await place(cog,b,"buy",25,110)
        except B.EconomyError as e: print("  ",e)
        print(f"  сделка {i+1}: s={s} b={b} котировка {bot.stocks.docs[0]['price']}")
        shares_ok(bot,"KORP")
    # спрос иссяк — продавцы сбивают цену до 90
    for i,(s,b) in enumerate([(12,11),(14,13),(11,15),(13,12),(15,14),(12,13)]):
        await place(cog,s,"sell",25,90)
        try: await place(cog,b,"buy",25,90)
        except B.EconomyError as e: print("  ",e)
    print("после падения спроса:", bot.stocks.docs[0]["price"])
    st=bot.stocks.docs[0]; print("прибыль", st["profit_pool"], "казна", st["treasury"], "объём", st["volume_total"])
    # дивиденды не создают деньги и не трогают резерв
    reserve=bot.economy.docs[0]["reserve"]; m0=total_money(bot)
    r=await cog.pay_dividends(G,"KORP"); print("дивиденды:", r)
    assert bot.economy.docs[0]["reserve"]==reserve and total_money(bot)==m0
    # справедливая цена и дрейф без торгов
    fair=B.fair_value(st["treasury"],st["supply"],await cog._profit_days(G,"KORP",7)); print("fair", round(fair,2))
    print("OK")
asyncio.run(main())
