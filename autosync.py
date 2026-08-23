from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests
from telebot.types import CallbackQuery, Message, InlineKeyboardButton as B, InlineKeyboardMarkup as K

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "SMMWay Price AutoSync"
VERSION = "1.0.0"

DESCRIPTION = """Автоматический пересчет цен SMMWay.
Округление происходит вниз.
Т.е. если цена в лоте выше, чем цена на панели — все будет работать.
команда для бота /smmsync"""

CREDITS = "@sv1cid3"
UUID = "a98d7dc2-54e6-47fe-87d9-509f87b1a0c7"
SETTINGS_PAGE = False
GITHUB = "https://github.com/uNoless/smmway_autosync"

logger = logging.getLogger("FPC.smmway_autosync")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "smm_config.json")
DEFAULT_CONFIG = {
    "api_key": "",
    "multiplier": 1.5,
    "update_interval": 43200,
    "threshold": 0.1
}

_STATE_WAIT_MULT = f"{UUID}_wait_mult"
_STATE_WAIT_KEY = f"{UUID}_wait_key"
_STATE_WAIT_THRESHOLD = f"{UUID}_wait_threshold"

SMM_ERROR_MESSAGES = {
    "user_inactive": "Аккаунт SMMWay заблокирован или не активирован. Проверьте профиль на сайте. (Не валид ключ)",
}

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CONFIG

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=3) 

@dataclass
class ParsedLot:
    lot_id: int
    service_id: int
    amount: int
    current_price: float
    multiplier: float = 1.5

class SmmWayAPIError(Exception):
    """Ошибка апи панельки беда с ключом и тд"""

class SmmWayClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://smmway.ru/api/v2"
        self.session = requests.session()
        
    def get_rates(self) -> dict[int, float]:
        params = {
            "action": "services",
            "key": self.api_key,
        }
        try:
            response = self.session.get(self.base_url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            match data:
                case {"error": error_msg}:
                    user_message = SMM_ERROR_MESSAGES.get(
                        error_msg, f'Ошибка API SMMWay: {error_msg}'
                    )
                    raise SmmWayAPIError(user_message)
                case list() as items:
                    return {
                        int(item["service"]): float(item["rate"])
                        for item in items
                        if "service" in item and "rate" in item
                    }
                case _:
                    return {}
        except (requests.RequestException, ValueError):
            return {}

def parse_lot(lot_id: int, description: str | None, price: float) -> ParsedLot | None:
    dct = dict(re.findall(r'(\w+):\s*([^\n\r]+)', description or ""))
    match dct:
        case {"smm": "on", "id": raw_id}:
            if dct.get("name", "way") != "way":
                return None
            try:
                return ParsedLot(
                    lot_id=lot_id,
                    service_id=int(raw_id),
                    amount=int(dct.get("am", 1)),
                    current_price=price,
                )
            except (ValueError, TypeError):
                return None
        case _:
            return None

def calculate_new_price(
        lot: ParsedLot,
        rate: float,
        cfg: dict
) -> float | None:
    mult = cfg.get("multiplier", 1.5)
    threshold = cfg.get("threshold", 0.1)

    new_price = round((rate / 1000) * lot.amount * mult, 2)

    if new_price - lot.current_price >= threshold:
        return new_price
    return None

def send_alert(cardinal: Cardinal, text: str, chat_id: int | None = None, reply_markup=None):
    if chat_id:
        try:
            cardinal.telegram.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        return
    for uid in cardinal.telegram.authorized_users:
        try:
            cardinal.telegram.bot.send_message(uid, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass

def save_lot_with_retry(account, lot_id: int, new_price: float, max_attempts: int = 3) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            fields = account.get_lot_fields(lot_id)
            fields.price = new_price
            fields.csrf_token = account.csrf_token
            fields.renew_fields()
            account.save_lot(fields)
            return True
        except Exception as e:
            if attempt == max_attempts:
                raise e
            time.sleep(1.0 * attempt)
    return False

def sync_once(cardinal: Cardinal, chat_id: int | None = None):
    config = load_config()
    client = SmmWayClient(api_key=config["api_key"])
    try:
        try:
            rates = client.get_rates()
        except SmmWayAPIError as e:
            logger.error(f"Ошибка API SMMway: {e}")
            send_alert(cardinal, f"⚠️ <b>{NAME}</b>: Ошибка API SMMWay:\n<code>{e}</code>", chat_id)
            return
        except Exception as e:
            logger.warning(f"Сетевой сбой при запросе цен: {e}")
            send_alert(cardinal, f"[{NAME}] Сетевой сбой при запросе цен: {e}", chat_id)
            return

        cardinal.update_lots_and_categories()

        updated_count = 0
        failed_lots = []

        for fp_lot in cardinal.tg_profile.get_common_lots():
            try:
                fields = cardinal.account.get_lot_fields(fp_lot.id)
                lot = parse_lot(fp_lot.id, fields.description_ru, float(fields.price))

                if not lot:
                    continue

                rate = rates.get(lot.service_id)
                if not rate:
                    continue

                new_price = calculate_new_price(lot, rate, config)

                if new_price is not None:
                    save_lot_with_retry(cardinal.account, fp_lot.id, new_price, max_attempts=3)
                    updated_count += 1

                time.sleep(0.75)

            except Exception as lot_err:
                logger.warning(f"Ошибка обработки лота {fp_lot.id}: {lot_err}")
                failed_lots.append(f"• Лот <code>#{fp_lot.id}</code>: {lot_err}")

        msg = f"✅ <b>{NAME}</b>: Обход завершен!\nОбновлено лотов: <b>{updated_count}</b>"

        if failed_lots:
            errors_text = "\n".join(failed_lots[:10])
            if len(failed_lots) > 10:
                errors_text += f"\n<i>...и еще {len(failed_lots) - 10} шт. (см. логи)</i>"

            msg += f"\n\n⚠️ <b>Ошибки ({len(failed_lots)}):</b>\n{errors_text}"

        send_alert(cardinal, msg, chat_id)

    except Exception as e:
        logger.error(f'Критическая ошибка: {e}')
        send_alert(cardinal, f'[{NAME}] Критическая ошибка: {e}', chat_id)

def updater_loop(cardinal: Cardinal):
    time.sleep(600)
    while True:
        cfg = load_config()
        if cfg.get("api_key"):
            sync_once(cardinal)
        time.sleep(cfg.get("update_interval", 43200))


def build_menu_text() -> str:
    config = load_config()
    key_preview = f"{config['api_key'][:6]}..." if config.get("api_key") else "<i>не задан</i>"
    return (
        f"⚙️ <b>{NAME}</b>\n\n"
        f"🔑 <b>API-ключ:</b> <code>{key_preview}</code>\n"
        f"💹 <b>Множитель:</b> <code>x{config.get('multiplier', 1.5)}</code>\n"
        f"🎯 <b>Порог:</b> <code>{config.get('threshold', 0.1)} ₽</code>\n"
        f"⏱ <b>Интервал:</b> <code>{config.get('update_interval', 43200)} сек.</code>"
    )

def build_menu_kb() -> K:
    kb = K()
    kb.row(
        B("🔑 Сменить ключ", callback_data=f"{UUID}_set_key"),
        B("💹 Множитель", callback_data=f"{UUID}_set_mult")
    )
    kb.row(
        B("🎯 Порог (₽)", callback_data=f"{UUID}_set_threshold"),
        B("▶️ Запустить сейчас", callback_data=f"{UUID}_run_now")
    )
    return kb

def build_cancel_kb() -> K:
    return K().add(B("❌ Отмена", callback_data=f"{UUID}_cancel_state"))

def handle_callbacks(c: CallbackQuery, cardinal: Cardinal):
    tg = cardinal.telegram
    bot = tg.bot
    if c.from_user.id not in tg.authorized_users:
        return

    act = c.data.replace(f"{UUID}_", "")
    match act:
        case "run_now":
            bot.answer_callback_query(c.id, "Запуск обхода...")
            threading.Thread(target=sync_once, args=(cardinal, c.message.chat.id), daemon=True).start()

        case "set_mult":
            bot.answer_callback_query(c.id)
            tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, _STATE_WAIT_MULT, {})
            try:
                bot.edit_message_text(
                    "✏️ <b>Введите новый множитель</b> (например, <code>1.8</code>):",
                    c.message.chat.id,
                    c.message.message_id,
                    parse_mode="HTML",
                    reply_markup=build_cancel_kb()
                )
            except Exception:
                pass

        case "set_key":
            bot.answer_callback_query(c.id)
            tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, _STATE_WAIT_KEY, {})
            try:
                bot.edit_message_text(
                    "✏️ <b>Введите API-ключ SMMWay</b> (60 символов):",
                    c.message.chat.id,
                    c.message.message_id,
                    parse_mode="HTML",
                    reply_markup=build_cancel_kb()
                )
            except Exception:
                pass

        case "set_threshold":
            bot.answer_callback_query(c.id)
            tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, _STATE_WAIT_THRESHOLD, {})
            try:
                bot.edit_message_text(
                    "✏️ <b>Введите порог срабатывания в рублях</b> (например, <code>0.1</code>):",
                    c.message.chat.id,
                    c.message.message_id,
                    parse_mode="HTML",
                    reply_markup=build_cancel_kb()
                )
            except Exception:
                pass

        case "cancel_state":
            bot.answer_callback_query(c.id, "Отменено")
            tg.clear_state(c.message.chat.id, c.from_user.id)
            try:
                bot.edit_message_text(
                    build_menu_text(),
                    c.message.chat.id,
                    c.message.message_id,
                    parse_mode="HTML",
                    reply_markup=build_menu_kb()
                )
            except Exception:
                pass


def on_mult_msg(m: Message, cardinal: Cardinal):
    tg = cardinal.telegram
    if not m.text:
        return
    raw = m.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError
    except ValueError:
        tg.bot.reply_to(m, "⚠️ <b>Некорректное число!</b> Введите положительное число (например, <code>1.8</code>):", parse_mode="HTML")
        return

    cfg = load_config()
    cfg["multiplier"] = val
    save_config(cfg)

    st = tg.get_state(m.chat.id, m.from_user.id) or {}
    mid = st.get("mid") or st.get("message_id")
    tg.clear_state(m.chat.id, m.from_user.id)

    try:
        tg.bot.delete_message(m.chat.id, m.message_id)
        if mid:
            tg.bot.edit_message_text(build_menu_text(), m.chat.id, mid, parse_mode="HTML", reply_markup=build_menu_kb())
        else:
            tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())
    except Exception:
        tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())


def on_key_msg(m: Message, cardinal: Cardinal):
    tg = cardinal.telegram
    if not m.text:
        return
    key = m.text.strip()
    
    if len(key) != 60:
        tg.bot.reply_to(
            m,
            f"⚠️ <b>Неверная длина API-ключа!</b>\n"
            f"Вы ввели <b>{len(key)}</b> символов, а должно быть ровно <b>60</b>.\n"
            f"Попробуйте ещё раз:",
            parse_mode="HTML"
        )
        return

    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)

    st = tg.get_state(m.chat.id, m.from_user.id) or {}
    mid = st.get("mid") or st.get("message_id")
    tg.clear_state(m.chat.id, m.from_user.id)

    try:
        tg.bot.delete_message(m.chat.id, m.message_id)
        if mid:
            tg.bot.edit_message_text(build_menu_text(), m.chat.id, mid, parse_mode="HTML", reply_markup=build_menu_kb())
        else:
            tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())
    except Exception:
        tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())


def on_threshold_msg(m: Message, cardinal: Cardinal):
    tg = cardinal.telegram
    if not m.text:
        return
    raw = m.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError
    except ValueError:
        tg.bot.reply_to(m, "⚠️ <b>Некорректное число!</b> Введите число порога в рублях (например, <code>0.1</code>):", parse_mode="HTML")
        return

    cfg = load_config()
    cfg["threshold"] = val
    save_config(cfg)

    st = tg.get_state(m.chat.id, m.from_user.id) or {}
    mid = st.get("mid") or st.get("message_id")
    tg.clear_state(m.chat.id, m.from_user.id)

    try:
        tg.bot.delete_message(m.chat.id, m.message_id)
        if mid:
            tg.bot.edit_message_text(build_menu_text(), m.chat.id, mid, parse_mode="HTML", reply_markup=build_menu_kb())
        else:
            tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())
    except Exception:
        tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb())


def init(cardinal: Cardinal):
    logger.info(f"🚀 {NAME} инициализация...")
    logger.info(f"✨ Разработчик: {CREDITS} | Репозиторий: {GITHUB}")

    threading.Thread(target=updater_loop, args=(cardinal,), daemon=True).start()

    tg = cardinal.telegram

    tg.add_command_to_menu("smmsync", "Настройки SMMWay Price AutoSync")
    tg.add_command_to_menu("smm", "Быстрое меню SMMWay")

    tg.msg_handler(
        lambda m: tg.bot.send_message(m.chat.id, build_menu_text(), parse_mode="HTML", reply_markup=build_menu_kb()),
        commands=["smmsync"]
    )

    tg.cbq_handler(
        lambda c: handle_callbacks(c, cardinal),
        func=lambda c: (c.data or "").startswith(f"{UUID}_")
    )

    tg.msg_handler(
        lambda m: on_mult_msg(m, cardinal),
        func=lambda m: m.content_type == "text"
        and m.from_user.id in tg.authorized_users
        and tg.check_state(m.chat.id, m.from_user.id, _STATE_WAIT_MULT)
        and (not m.text or not m.text.strip().startswith("/")),
    )

    tg.msg_handler(
        lambda m: on_key_msg(m, cardinal),
        func=lambda m: m.content_type == "text"
        and m.from_user.id in tg.authorized_users
        and tg.check_state(m.chat.id, m.from_user.id, _STATE_WAIT_KEY)
        and (not m.text or not m.text.strip().startswith("/")),
    )

    tg.msg_handler(
        lambda m: on_threshold_msg(m, cardinal),
        func=lambda m: m.content_type == "text"
        and m.from_user.id in tg.authorized_users
        and tg.check_state(m.chat.id, m.from_user.id, _STATE_WAIT_THRESHOLD)
        and (not m.text or not m.text.strip().startswith("/")),
    )


BIND_TO_PRE_INIT = [init]
BIND_TO_INIT = []
BIND_TO_DELETE = ["CONFIG_FILE"]
