from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import dataclass
import time
import threading
import re
import requests
import json
import os
import logging

from telebot import types
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "SMMWay Price AutoSync"
VERSION = "1.0.0"

DESCRIPTION = """Автоматический пересчет цен SMMWay.
Округление происходит вниз.
Т.е. если цена в лоте выше, чем цена на панели — все будет работать."""

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

SMM_ERROR_MESSAGES = {
    "user_inactive": "Аккаунт SMMWay заблокирован или не активирован. Проверьте профиль на сайте. (Не валид ключ)",
}

bot = None
cardinal_ref = None

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

config = load_config()

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

    new_price = round((rate / 1000) * lot.amount * mult, 4)

    if new_price - lot.current_price >= threshold:
        return new_price
    return None

def send_alert(cardinal: Cardinal, text: str, chat_id: int | None = None, reply_markup=None):
    if chat_id and bot:
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass
        return
    for uid in cardinal.telegram.authorized_users:
        try:
            bot.send_message(uid, text, parse_mode="HTML", reply_markup=reply_markup)
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
    cfg = load_config()
    client = SmmWayClient(api_key=cfg["api_key"])
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

                new_price = calculate_new_price(lot, rate, cfg)

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


# ===================== ТЕЛЕГРАМ МЕНЮ И ШАГИ =====================

def send_settings_menu(message, cardinal: Cardinal):
    cfg = load_config()
    key_preview = f"{cfg['api_key'][:6]}..." if cfg.get("api_key") else "<i>не задан</i>"
    
    text = (
        f"⚙️ <b>{NAME}</b>\n\n"
        f"🔑 <b>API-ключ:</b> <code>{key_preview}</code>\n"
        f"💹 <b>Множитель:</b> <code>x{cfg.get('multiplier', 1.5)}</code>\n"
        f"🎯 <b>Порог:</b> <code>{cfg.get('threshold', 0.1)} ₽</code>\n"
        f"⏱ <b>Интервал:</b> <code>{cfg.get('update_interval', 43200)} сек.</code>"
    )
    
    kb = K()
    kb.row(
        B("🔑 Сменить ключ", callback_data=f"{UUID}_set_key"),
        B("💹 Множитель", callback_data=f"{UUID}_set_mult")
    )
    kb.row(
        B("🎯 Порог (₽)", callback_data=f"{UUID}_set_threshold"),
        B("▶️ Запустить сейчас", callback_data=f"{UUID}_run_now")
    )
    
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=kb
    )

def process_multiplier(m: types.Message):
    if m.text and m.text.lower() == "/cancel":
        bot.send_message(m.chat.id, "❌ Ввод отменен")
        send_settings_menu(m, cardinal_ref)
        return

    raw_text = (m.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        new_mult = float(raw_text)
        if new_mult <= 0:
            raise ValueError
        cfg = load_config()
        cfg["multiplier"] = new_mult
        save_config(cfg)
        bot.send_message(m.chat.id, f"✅ Множитель успешно изменен на <b>x{new_mult}</b>!", parse_mode="HTML")
        send_settings_menu(m, cardinal_ref)
    except ValueError:
        bot.send_message(m.chat.id, "⚠️ Некорректное число. Введите положительное число (например, <code>1.5</code>) или /cancel:", parse_mode="HTML")
        bot.register_next_step_handler_by_chat_id(m.chat.id, process_multiplier)

def process_api_key(m: types.Message):
    if m.text and m.text.lower() == "/cancel":
        bot.send_message(m.chat.id, "❌ Ввод отменен")
        send_settings_menu(m, cardinal_ref)
        return

    key = (m.text or "").strip()
    if len(key) != 60:
        bot.send_message(m.chat.id, f"⚠️ Неверный ключ (длина API-ключа SMMWay должна быть 60 символов, вы ввели {len(key)}). Попробуйте снова или /cancel:")
        bot.register_next_step_handler_by_chat_id(m.chat.id, process_api_key)
        return

    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)
    bot.send_message(m.chat.id, "✅ API-ключ успешно сохранен!", parse_mode="HTML")
    send_settings_menu(m, cardinal_ref)

def process_threshold(m: types.Message):
    if m.text and m.text.lower() == "/cancel":
        bot.send_message(m.chat.id, "❌ Ввод отменен")
        send_settings_menu(m, cardinal_ref)
        return

    raw_text = (m.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        new_thres = float(raw_text)
        if new_thres <= 0:
            raise ValueError
        cfg = load_config()
        cfg["threshold"] = new_thres
        save_config(cfg)
        bot.send_message(m.chat.id, f"✅ Погрешность успешно изменена на <b>{new_thres} ₽</b>!", parse_mode="HTML")
        send_settings_menu(m, cardinal_ref)
    except ValueError:
        bot.send_message(m.chat.id, "⚠️ Некорректное число. Введите положительное число (например, <code>0.1</code>) или /cancel:", parse_mode="HTML")
        bot.register_next_step_handler_by_chat_id(m.chat.id, process_threshold)


def init(cardinal: Cardinal):
    global bot, cardinal_ref
    cardinal_ref = cardinal
    bot = cardinal.telegram.bot

    logger.info(f"🚀 {NAME} инициализация...")
    logger.info(f"✨ Разработчик: {CREDITS} | Репозиторий: {GITHUB}")

    threading.Thread(target=updater_loop, args=(cardinal,), daemon=True).start()

    cardinal.add_telegram_commands(UUID, [
        ("smmsync", "⚙️ Меню автосинхронизации цен", True)
    ])

    @bot.message_handler(commands=["smmsync"])
    def handle_cmd(m: types.Message):
        if m.from_user.id in cardinal.telegram.authorized_users:
            send_settings_menu(m, cardinal)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith(f"{UUID}_"))
    def handle_callbacks(c: types.CallbackQuery):
        if c.from_user.id not in cardinal.telegram.authorized_users:
            return
        
        act = c.data.replace(f"{UUID}_", "")
        match act:
            case "run_now":
                bot.answer_callback_query(c.id, "Запуск обхода...")
                threading.Thread(target=sync_once, args=(cardinal, c.message.chat.id), daemon=True).start()

            case "set_mult":
                bot.answer_callback_query(c.id)
                bot.clear_step_handler_by_chat_id(c.message.chat.id)
                bot.send_message(c.message.chat.id, "✏️ Введите новый множитель (например, <code>1.8</code>)\nОтправьте /cancel для отмены:", parse_mode="HTML")
                bot.register_next_step_handler_by_chat_id(c.message.chat.id, process_multiplier)

            case "set_key":
                bot.answer_callback_query(c.id)
                bot.clear_step_handler_by_chat_id(c.message.chat.id)
                bot.send_message(c.message.chat.id, "✏️ Введите API-ключ SMMWay (60 символов)\nОтправьте /cancel для отмены:", parse_mode="HTML")
                bot.register_next_step_handler_by_chat_id(c.message.chat.id, process_api_key)

            case "set_threshold":
                bot.answer_callback_query(c.id)
                bot.clear_step_handler_by_chat_id(c.message.chat.id)
                bot.send_message(c.message.chat.id, "✏️ Введите погрешность в рублях (например, <code>0.1</code>)\nОтправьте /cancel для отмены:", parse_mode="HTML")
                bot.register_next_step_handler_by_chat_id(c.message.chat.id, process_threshold)


BIND_TO_PRE_INIT = [init]
BIND_TO_INIT = []
BIND_TO_DELETE = [CONFIG_FILE]
