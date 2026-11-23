import sys, types, copy, asyncio, uuid, importlib.util, os
from datetime import datetime, timezone

# ---------- заглушки внешних библиотек ----------
class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, n): return _Any()
def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m

class Embed:
    def __init__(self, **k): self.__dict__.update(k); self.fields=[]
    def add_field(self, name, value, inline=False): self.fields.append((name, value))
    def set_footer(self, **k): pass
    def set_thumbnail(self, **k): pass
    def set_author(self, **k): pass
    def set_image(self, **k): pass
class Color:
    @staticmethod
    def green(): return 1
    red = orange = blurple = gold = green
class _Deco:
    def __init__(self, fn): self.fn = fn
    def command(self, *a, **k): return lambda f: f
def _decorator(*a, **k):
    if len(a) == 1 and callable(a[0]) and not k: return a[0]
    return lambda f: f
def hybrid_group(*a, **k): return lambda f: _Deco(f) if False else _grp(f)
def _grp(f):
    f.command = lambda *a, **k: (lambda g: g)
    return f
def loop(*a, **k):
    def deco(f):
        f.before_loop = lambda g: g
        return f
    return deco
class Cog: pass
class Bot:
    def __init__(self, *a, **k): pass
class UIButton:
    def __init__(self, *a, **k):
        self.disabled = False; self.view = None; self.__dict__.update(k)
class UIView:
    def __init__(self, *a, timeout=180, **k):
        self.timeout = timeout; self.children = []; self._stopped = False
        for name in dir(type(self)):
            fn = getattr(type(self), name, None)
            if callable(fn) and hasattr(fn, "__ui_button__"):
                item = UIButton(**fn.__ui_button__); item.view = self
                item.callback = (lambda f, it: (lambda interaction: f(self, interaction, it)))(fn, item)
                setattr(self, name, item); self.children.append(item)
    def add_item(self, item): item.view = self; self.children.append(item)
    def stop(self): self._stopped = True
    def is_finished(self): return self._stopped
class UIModal:
    def __init__(self, *a, title=None, timeout=None, **k): self.title = title; self.children = []
    def add_item(self, item): self.children.append(item)
class UITextInput:
    def __init__(self, **k): self.value = None; self.__dict__.update(k)
def _ui_button(**kw):
    def d(f): f.__ui_button__ = kw; return f
    return d
discord = mod("discord", Embed=Embed, Color=Color, Interaction=type("Interaction", (), {}),
              Intents=type("Intents", (), {"none": classmethod(lambda c: c())}),
              ButtonStyle=_Any(), NotFound=Exception, Forbidden=Exception, HTTPException=Exception,
              ClientException=Exception, File=_Any, Member=_Any, Guild=_Any,
              ui=types.SimpleNamespace(Button=UIButton, View=UIView, Modal=UIModal, TextInput=UITextInput,
                                       Select=type("Select", (), {"__init__": lambda s,*a,**k: None}),
                                       button=_ui_button, select=_decorator),
              SelectOption=_Any, TextStyle=_Any())
app_commands = types.SimpleNamespace(describe=_decorator, Choice=_Any, choices=_decorator, AppCommandError=Exception)
discord.app_commands = app_commands
discord.__getattr__ = lambda name: type(name, (), {})

commands = types.SimpleNamespace(Cog=Cog, Bot=Bot, hybrid_group=hybrid_group, hybrid_command=_decorator,
    command=_decorator, has_permissions=_decorator, when_mentioned_or=lambda *a: None, Context=_Any,
    MissingPermissions=Exception, NoPrivateMessage=Exception, MissingRequiredArgument=Exception,
    BadArgument=Exception, BadUnionArgument=Exception, CheckFailure=Exception, CommandNotFound=Exception,
    CommandInvokeError=Exception, group=_decorator, Greedy=_Any, check=_decorator, cooldown=_decorator,
    BucketType=_Any(), Converter=object, MemberConverter=object)
mod("discord.ext"); ext_c = mod("discord.ext.commands", **commands.__dict__)
ext_c.guild_only = _decorator
ext_c.__getattr__ = lambda name: _decorator
ext_t = mod("discord.ext.tasks", loop=loop)
sys.modules["discord.ext"].commands = ext_c; sys.modules["discord.ext"].tasks = ext_t
mod("discord.app_commands", **app_commands.__dict__)
mod("aiohttp"); mod("aiohttp.web")
sys.modules["aiohttp"].web = sys.modules["aiohttp.web"]
mod("motor"); mod("motor.motor_asyncio", AsyncIOMotorClient=_Any)
class DuplicateKeyError(Exception): pass
class OperationFailure(Exception): code = 0
mod("pymongo"); mod("pymongo.errors", DuplicateKeyError=DuplicateKeyError, OperationFailure=OperationFailure)

# ---------- поддельная MongoDB ----------
YIELD = False        # True — каждая операция отдаёт управление циклу событий (имитация конкурентности)
def _get(doc, key): return doc.get(key)
def _match(doc, flt):
    for k, c in flt.items():
        if k == "$or":
            if not any(_match(doc, f) for f in c): return False
            continue
        v = doc.get(k)
        if isinstance(c, dict) and any(x.startswith("$") for x in c):
            for op, a in c.items():
                if op == "$gte":
                    if v is None or not v >= a: return False
                elif op == "$gt":
                    if v is None or not v > a: return False
                elif op == "$lte":
                    if v is None or not v <= a: return False
                elif op == "$lt":
                    if v is None or not v < a: return False
                elif op == "$ne":
                    if v == a: return False
                elif op == "$in":
                    if v not in a: return False
                elif op == "$exists":
                    if (k in doc) != a: return False
                else: raise NotImplementedError(op)
        else:
            if v != c: return False
    return True

class Res:
    def __init__(self, m=0, mod=0): self.matched_count=m; self.modified_count=mod
class Cursor:
    def __init__(self, docs): self.docs = docs
    def sort(self, key, direction=None):
        keys = key if isinstance(key, list) else [(key, direction)]
        for k, d in reversed(keys):
            self.docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=(d == -1))
        return self
    def limit(self, n): self.docs = self.docs[:n]; return self
    async def to_list(self, n): return [copy.deepcopy(d) for d in self.docs[:n]]
    def __aiter__(self):
        self._it = iter([copy.deepcopy(d) for d in self.docs]); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration

class Coll:
    def __init__(self, name, uniq=()): self.name=name; self.docs=[]; self.uniq=uniq
    def _check_unique(self, doc):
        for u in self.uniq:
            for d in self.docs:
                if d is not doc and all(d.get(k)==doc.get(k) for k in u): raise DuplicateKeyError(self.name)
    async def find_one(self, flt=None, projection=None, session=None, sort=None):
        if YIELD: await asyncio.sleep(0)
        docs = [d for d in self.docs if _match(d, flt or {})]
        if sort:
            for k, dr in reversed(sort): docs.sort(key=lambda x: x.get(k), reverse=(dr == -1))
        return copy.deepcopy(docs[0]) if docs else None
    def find(self, flt=None, projection=None, session=None):
        return Cursor([d for d in self.docs if _match(d, flt or {})])
    async def insert_one(self, doc, session=None):
        if YIELD: await asyncio.sleep(0)
        doc = copy.deepcopy(doc); doc.setdefault("_id", uuid.uuid4().hex)
        if any(d["_id"] == doc["_id"] for d in self.docs): raise DuplicateKeyError(self.name)
        self.docs.append(doc); self._check_unique(doc)
    async def insert_many(self, docs, session=None):
        for d in docs: await self.insert_one(d)
    async def delete_one(self, flt, session=None):
        for i, d in enumerate(self.docs):
            if _match(d, flt): del self.docs[i]; return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)
    async def delete_many(self, flt, session=None):
        n = len(self.docs); self.docs = [d for d in self.docs if not _match(d, flt)]
        return types.SimpleNamespace(deleted_count=n - len(self.docs))
    async def count_documents(self, flt, session=None):
        return len([d for d in self.docs if _match(d, flt)])
    def _apply(self, doc, upd, insert=False):
        before = copy.deepcopy(doc)
        def _nest(d, key):
            parts = key.split(".")
            for p in parts[:-1]: d = d.setdefault(p, {})
            return d, parts[-1]
        for k, v in upd.get("$inc", {}).items():
            d, last = _nest(doc, k); d[last] = (d.get(last) or 0) + v
        for k, v in upd.get("$set", {}).items():
            d, last = _nest(doc, k); d[last] = v
        if insert:
            for k, v in upd.get("$setOnInsert", {}).items(): doc[k] = v
        for k, v in upd.get("$push", {}).items():
            arr = doc.setdefault(k, [])
            if isinstance(v, dict) and "$each" in v:
                arr.extend(v["$each"])
                if "$slice" in v and v["$slice"] < 0: del arr[:max(0, len(arr)+v["$slice"])]
            else: arr.append(v)
        return doc != before
    async def update_one(self, flt, upd, upsert=False, session=None):
        if YIELD: await asyncio.sleep(0)
        for d in self.docs:
            if _match(d, flt):
                changed = self._apply(d, upd)
                return Res(1, 1 if changed else 0)
        if upsert:
            doc = {k: v for k, v in flt.items() if not (isinstance(v, dict) or k.startswith("$"))}
            doc.setdefault("_id", uuid.uuid4().hex)
            self._apply(doc, upd, insert=True); self.docs.append(doc); self._check_unique(doc)
            return Res(0, 0)
        return Res(0, 0)
    def aggregate(self, pipe, session=None):
        docs = [d for d in self.docs]
        for st in pipe:
            if "$match" in st: docs = [d for d in docs if _match(d, st["$match"])]
            elif "$group" in st:
                g = st["$group"]; groups = {}
                for d in docs:
                    key = None if g["_id"] is None else d.get(g["_id"][1:])
                    row = groups.setdefault(key, {"_id": key})
                    for f, spec in g.items():
                        if f == "_id": continue
                        field = spec["$sum"][1:]
                        row[f] = row.get(f, 0) + (d.get(field) or 0)
                docs = list(groups.values())
            elif "$sort" in st:
                (k, dr), = st["$sort"].items(); docs.sort(key=lambda x: x[k], reverse=(dr == -1))
            elif "$limit" in st: docs = docs[:st["$limit"]]
        return Cursor(docs)

class FakeDB:
    NAMES = ["users","economy","funds","tx","stocks","stock_holdings","stock_orders","stock_trades",
             "stock_dividends","stock_payouts","guild_settings","active_games","casino_stats"]
    def __init__(self):
        self.c = {n: Coll(n) for n in self.NAMES}
        self.c["users"].uniq = [("guild_id","user_id")]
        self.c["stock_holdings"].uniq = [("guild_id","user_id","ticker")]
    def snapshot(self): return {n: copy.deepcopy(c.docs) for n, c in self.c.items()}
    def restore(self, snap):
        for n, d in snap.items(): self.c[n].docs = d
