import sys, os, asyncio, importlib.util, types, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H
spec = importlib.util.spec_from_file_location("botmod", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.py"))
B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)
G = 1

class FakeBot(B.EconomyBot):
    def __init__(self):
        self.db = H.FakeDB(); c = self.db.c
        self.db_users, self.economy, self.funds, self.tx = c["users"], c["economy"], c["funds"], c["tx"]
        self.stocks, self.stock_holdings, self.stock_orders = c["stocks"], c["stock_holdings"], c["stock_orders"]
        self.stock_trades, self.stock_dividends, self.stock_payouts = c["stock_trades"], c["stock_dividends"], c["stock_payouts"]
        self.settings, self.active_games, self.casino_stats = c["guild_settings"], c["active_games"], c["casino_stats"]
        self._settings_cache = {}; self._infl_cache = {}
    async def run_tx(self, fn):
        snap = self.db.snapshot()
        try: return await fn(None)
        except BaseException:
            self.db.restore(snap); raise

class Ctx:
    def __init__(self, gid, uid):
        self.guild = types.SimpleNamespace(id=gid, name="G")
        self.author = types.SimpleNamespace(id=uid, display_name=f"u{uid}", display_avatar=types.SimpleNamespace(url="x"))
        self.interaction = None; self.sent = []
    async def send(self, *a, **k): self.sent.append(k.get("embed")); return None

def user(bot, uid, gid=G):
    return next((d for d in bot.db_users.docs if d["user_id"] == uid and d["guild_id"] == gid), None)
def cash(bot, uid, gid=G): return user(bot, uid, gid)["cash"]
def reserve(bot, gid=G): return bot.economy.docs[[d["_id"] for d in bot.economy.docs].index(bot.cb_id(gid))]["reserve"]
def money(bot, gid=G):
    return (sum(d.get("cash", 0) + d.get("bank", 0) for d in bot.db_users.docs if d["guild_id"] == gid)
            + reserve(bot, gid) + sum(d.get("balance", 0) for d in bot.funds.docs if d["guild_id"] == gid))
async def give(bot, uid, amount, gid=G):
    """Выдача из резерва (масса не меняется)."""
    await bot.ensure_user(gid, uid); await bot.change_cash(gid, uid, amount)
    await bot.change_reserve(gid, -amount, allow_negative=True)

# ---- фейки Discord ----
class FMsg:
    n = 1000
    def __init__(self):
        FMsg.n += 1; self.id = FMsg.n; self.edits = []; self.embed = None; self.view = None
    async def edit(self, **k):
        self.edits.append(k)
        if "embed" in k: self.embed = k["embed"]
        if "view" in k: self.view = k["view"]
class FChannel:
    def __init__(self): self.partial = {}
    def get_partial_message(self, mid): return self.partial.setdefault(mid, FMsg())
class FResp:
    def __init__(self, i): self.i, self.done = i, False
    def is_done(self): return self.done
    async def defer(self, **k): self.done = True
    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.done = True
        if ephemeral: self.i.replies.append(content)
        else:
            m = FMsg(); m.embed, m.view = embed, view; self.i.sent_msg = m
            self.i.channel.partial[m.id] = m
    async def send_modal(self, modal): self.done = True; self.i.modal = modal
    async def edit_message(self, **k): self.done = True; await self.i.message.edit(**k)
class FInter:
    def __init__(self, uid, channel, message=None, gid=G):
        self.user = types.SimpleNamespace(id=uid, display_name=f"u{uid}"); self.guild_id = gid
        self.channel = channel; self.message = message; self.replies = []; self.modal = None; self.sent_msg = None
        self.response = FResp(self)
        self.followup = types.SimpleNamespace(send=self._followup)
    async def _followup(self, content=None, **k): self.replies.append(content)
    async def original_response(self): return self.sent_msg
    async def edit_original_response(self, **k): await self.message.edit(**k)
