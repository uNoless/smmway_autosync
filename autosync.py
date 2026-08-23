from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import dataclass
import time
import threading
import re
import requests
import json
import os

from telebot.types import CallbackQuery, InlineKeyboardMarkup as K, InlineKeyboardButton as B, Message

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "SMMWay Price AutoSync"
VERSION = "1.0.0"

DESCRIPTION = """Автоматический пересчет цен SMMWay.
Округление происходит вниз.
Т.е. если цена в лоте выше, чем цена на панели — все будет работать."""

CREDITS = "@adefstar"
UUID = "a98d7dc2-54e6-47fe-87d9-509f87b1a0c7"
SETTINGS_PAGE = True

CONFIG_FILE = "smm_config.json"
DEFAULT_CONFIG = {
    "api_key": "",
    "multiplier": 1.5,
    "update_interval": 43200,
    "threshold": 0.1
}

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

    new_price = round((rate / 1000) * lot.amount * mult, 2)

    if new_price - lot.current_price >= threshold:
        return new_price
    return None

GITHUB = 

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

def sync_once(cardinal: Cardinal, chat_id: int | None = None):
    config = load_config()
    client = SmmWayClient(api_key=config["api_key"])
    try:
        try:
            rates = client.get_rates()
        except SmmWayAPIError as e:

            cardinal.logger.error(f"[{NAME}] Ошибка API SMMway: {e}")
            send_alert(cardinal, f"⚠️ <b>{NAME}</b>: Ошибка API SMMWay:\n<code>{e}</code>", chat_id)
            return
        except Exception as e:

            cardinal.logger.warning(f"[{NAME}] Сетевой сбой при запросе цен: {e}")
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

                # Если цена изменилась — сохраняем через retry:
                if new_price is not None:
                    save_lot_with_retry(cardinal.account, fp_lot.id, new_price, max_attempts=3)
                    updated_count += 1

                time.sleep(0.75)

            except Exception as lot_err:
                cardinal.logger.warning(f"[{NAME}] Ошибка обработки лота {fp_lot.id}: {lot_err}")
                failed_lots.append(f"• Лот <code>#{fp_lot.id}</code>: {lot_err}")
                

        msg = f"✅ <b>{NAME}</b>: Обход завершен!\nОбновлено лотов: <b>{updated_count}</b>"

        if failed_lots:
            errors_text = "\n".join(failed_lots[:10])
            if len(failed_lots) > 10:
                errors_text += f"\n<i>...и еще {len(failed_lots) - 10} шт. (см. логи)</i>"

            msg += f"\n\n⚠️ <b>Ошибки ({len(failed_lots)}):</b>\n{errors_text}"

        send_alert(cardinal, msg, chat_id)

    except Exception as e:
        cardinal.logger.error(f'[{NAME}] Критическая ошибка: {e}')
        send_alert(cardinal, f'[{NAME}] Критическая ошибка: {e}', chat_id)
    

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
def updater_loop(cardinal: Cardinal):
    while True:
        sync_once(cardinal,)
        time.sleep(load_config().get("update_interval", 43200))

def init_price_checker(cardinal: Cardinal, *args):
    threading.Thread(target=updater_loop, args=(cardinal,), daemon=True).start()

    cardinal.telegram.cbq_handler(
        lambda c: open_settings(c, cardinal),
        func=lambda c: c.data == UUID
    )

    cardinal.telegram.cbq_handler(
        lambda c: handle_callbacks(c, cardinal),
        func=lambda c: c.data.startswith(f"{UUID}_")
    )

    cardinal.telegram.bot.register_message_handler(
        lambda msg: handle_msg(msg, cardinal),
        content_types=["text"]
    )

    cardinal.logger.info(f"[{NAME}] Плагин успешно инициализирован.")

def open_settings(c: CallbackQuery, cardinal: Cardinal):
    config = load_config()
    text = (
        f"⚙️ <b>Настройки {NAME}</b>\n\n"
        f"🔑 <b>API-ключ:</b> <code>{config['api_key'][:6]}...</code>\n"
        f"💹 <b>Множитель:</b> <code>x{config['multiplier']}</code>\n"
        f"⏱ <b>Интервал:</b> <code>{config['update_interval']} сек.</code>\n"
    )
    kb = K().row(
        B("🔑 Сменить ключ", callback_data=f"{UUID}_set_key"),
        B("💹 Множитель", callback_data=f"{UUID}_set_mult"),
        B("🎯 Сменить погрешность", callback_data=f"{UUID}_set_threshold")
    ).row(
        B("▶️ Запустить сейчас", callback_data=f"{UUID}_run_now")
    )
    cardinal.telegram.bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")

def handle_callbacks(c: CallbackQuery, cardinal: Cardinal):
    if c.from_user.id not in cardinal.telegram.authorized_users:
        return
    act = c.data.replace(f"{UUID}_", "")
    match act:
        case "run_now":
            cardinal.telegram.bot.answer_callback_query(c.id, "Запуск обхода...")
            threading.Thread(target=sync_once,args=(cardinal, c.message.chat.id), daemon=True).start()

        case "set_mult":
            cardinal.telegram.bot.answer_callback_query(c.id)
            cardinal.telegram.set_state(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                user_id=c.from_user.id,
                state=f'{UUID}_waiting_mult'
            )

            cancel_kb = K()
            cancel_kb.add(B("❌ Отмена", callback_data=f"{UUID}_cancel_state"))
            send_alert(cardinal, "✏️ Введите новый множитель (например, <code>1.8</code>):", chat_id=c.message.chat.id, reply_markup=cancel_kb)

        case "cancel_state":
            cardinal.telegram.bot.answer_callback_query(c.id, "Отменено")
            cardinal.telegram.clear_state(c.message.chat.id, c.from_user.id)
            open_settings(c, cardinal)

        case "set_key":
            cardinal.telegram.bot.answer_callback_query(c.id)
            cardinal.telegram.set_state(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                user_id=c.from_user.id,
                state=f'{UUID}_waiting_key'
            )

            cancel_kb = K()
            cancel_kb.add(B("❌ Отмена", callback_data=f"{UUID}_cancel_state"))
            send_alert(cardinal, "✏️ Введите API ключ:", chat_id=c.message.chat.id, reply_markup=cancel_kb)

        case "set_threshold":
            cardinal.telegram.bot.answer_callback_query(c.id)
            cardinal.telegram.set_state(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                user_id=c.from_user.id,
                state=f'{UUID}_waiting_threshold'
            )

            cancel_kb = K()
            cancel_kb.add(B("❌ Отмена", callback_data=f"{UUID}_cancel_state"))
            send_alert(cardinal, "✏️ Введите погрешность:", chat_id=c.message.chat.id, reply_markup=cancel_kb)
            

def handle_msg(m: Message, cardinal: Cardinal):
    if m.from_user.id not in cardinal.telegram.authorized_users:
        return

    state = cardinal.telegram.get_state(m.chat.id, m.from_user.id)
    if state == f"{UUID}_waiting_mult":
        raw_text = m.text.strip().replace(",", ".")
        try:
            new_mult = float(raw_text)
            if new_mult <= 0:
                raise ValueError
        except ValueError:
            send_alert(cardinal, "⚠️ Некорректное число. Введите положительное число (например, <code>1.5</code>):",chat_id=m.chat.id)
            return

        cfg = load_config()
        cfg["multiplier"] = new_mult
        save_config(cfg)

        cardinal.telegram.clear_state(m.chat.id, m.from_user.id)
        send_alert(cardinal, f"✅ Множитель успешно изменен на <b>x{new_mult}</b>!", chat_id=m.chat.id)

    elif state == f"{UUID}_waiting_key":
        key = m.text.strip()
        if len(key) != 60:
            send_alert(cardinal, '⚠️ Неверный ключ', chat_id=m.chat.id)
            return

        cfg = load_config()
        cfg["api_key"] = key
        save_config(cfg)
    
        cardinal.telegram.clear_state(m.chat.id, m.from_user.id)
        send_alert(cardinal, "✅ Ключ успешно изменен!", chat_id=m.chat.id)

    elif state == f'{UUID}_waiting_threshold':
        raw_text = m.text.strip().replace(",", ".")
        try:
            new_thres = float(raw_text)
            if new_thres <= 0:
                raise ValueError
        except ValueError:
            send_alert(cardinal, "⚠️ Некорректное число. Введите положительное число (например, <code>0.1</code>):",chat_id=m.chat.id)
            return

        cfg = load_config()
        cfg["threshold"] = new_thres
        save_config(cfg)

        cardinal.telegram.clear_state(m.chat.id, m.from_user.id)
        send_alert(cardinal, f"✅ Погрешность успешно изменена на <b>x{new_thres}</b>!", chat_id=m.chat.id)


BIND_TO_INIT = [init_price_checker]
BIND_TO_DELETE = ["smm_config.json"]
