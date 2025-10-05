import os
import time
import tempfile
import shutil
import threading
import json
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import tostring
from PIL import ImageGrab, Image
import requests
import telebot
from requests import session
from selenium import webdriver
from selenium.webdriver import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from telebot import types
import win32clipboard
# ----------------------
# Настройки
# ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or "7651581016:AAEMIo98sQBePF9J13SJ3ePVzmTFjvEpi88"
HEADLESS = False
PHONE_CACHE_FILE = "phone_cache.json"
COOKIES_DIR = "cookies_by_chat"
LOCALSTORAGE_DIR = "localstorage_by_chat"
PROFILES_DIR = r"E:\chrome_profiles"
BASE_URL = "https://web.max.ru"
CHROME_DRIVER_PATH = r"C:\Tools\chromedriver\chromedriver.exe"

os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(LOCALSTORAGE_DIR, exist_ok=True)
os.makedirs(PROFILES_DIR, exist_ok=True)

# ----------------------
# Инициализация бота
# ----------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Сессии: chat_id -> {phone, driver, tempdir, awaiting_step, last_action_time, timer_thread}
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TIMEOUT = 120
COMMAND_COOLDOWN = {
    "setphone":600,
    "checkmax":180
}
LAST_COMMAND_FILE = "last_command_time.json"

# Загрузка данных с последнего вызова команд
def load_last_command_times():
    if os.path.exists(LAST_COMMAND_FILE):
        try:
            with open(LAST_COMMAND_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

# Сохранение данных с последнего вызова команд
def save_last_command_times(data):
    try:
        with open(LAST_COMMAND_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения last_command_time: {e}")

# Проверка, можно ли выполнять команду
def can_execute_command(chat_id, command_name):
    last_times = load_last_command_times()
    key = f"{chat_id}_{command_name}"

    now = time.time()
    if key in last_times:
        elapsed = now - last_times[key]
        if elapsed < COMMAND_COOLDOWN[command_name]:
            return False, int(COMMAND_COOLDOWN[command_name] - elapsed)
    # Обновляем время выполнения
    last_times[key] = now
    save_last_command_times(last_times)
    return True, 0
# ----------------------
# Кэширование телефонов
# ----------------------
def load_phone_cache():
    if os.path.exists(PHONE_CACHE_FILE):
        try:
            with open(PHONE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_phone_cache():
    cache = {}
    with SESSIONS_LOCK:
        for chat_id, session in SESSIONS.items():
            if session.get("phone"):
                cache[str(chat_id)] = session["phone"]
    try:
        with open(PHONE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения кэша телефонов: {e}")

def restore_phone_cache():
    cache = load_phone_cache()
    with SESSIONS_LOCK:
        for chat_id_str, phone in cache.items():
            chat_id = int(chat_id_str)
            if chat_id not in SESSIONS:
                SESSIONS[chat_id] = {"phone": phone, "driver": None, "tempdir": None}
            else:
                SESSIONS[chat_id]["phone"] = phone

# ----------------------
# Таймер активности
# ----------------------
def reset_activity_timer(chat_id):
    """Сбрасывает таймер активности пользователя"""
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session:
            return
        session["last_action_time"] = time.time()

        if "timer_thread" in session and session["timer_thread"].is_alive():
            return

        def watcher():
            while True:
                time.sleep(3)
                with SESSIONS_LOCK:
                    s = SESSIONS.get(chat_id)
                    if not s:
                        return
                    last_action = s.get("last_action_time", 0)
                    print(time.time() - last_action)
                if time.time() - last_action > SESSION_TIMEOUT and s.get("awaiting_step"):
                    bot.send_message(chat_id, "⏳ Время ожидания истекло. Сессия закрыта.")
                    safe_quit_session_for_chat(chat_id)
                    return
                elif not s.get("awaiting_step"):
                    print("STOP")
                    return

        t = threading.Thread(target=watcher, daemon=True)
        session["timer_thread"] = t
        t.start()

# ----------------------
# Cookies / LocalStorage / Profiles
# ----------------------
def profile_path_for_chat(chat_id):
    return os.path.join(PROFILES_DIR, f"profile_{chat_id}")

def cookie_path_for_chat(chat_id):
    return os.path.join(COOKIES_DIR, f"{chat_id}.json")

def localstorage_path_for_chat(chat_id):
    return os.path.join(LOCALSTORAGE_DIR, f"{chat_id}.json")

def save_cookies_for_chat(driver, chat_id):
    try:
        cookies = driver.get_cookies()
        with open(cookie_path_for_chat(chat_id), "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"Cookies saved for {chat_id}")
    except Exception as e:
        print(f"Не удалось сохранить cookies: {e}")

def load_cookies_for_chat(driver, chat_id, base_url=BASE_URL):
    path = cookie_path_for_chat(chat_id)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        driver.get(base_url)
        for c in cookies:
            cookie = {k: c[k] for k in ("name", "value", "domain", "path", "expiry", "secure", "httpOnly") if k in c}
            try:
                driver.add_cookie(cookie)
            except Exception:
                cookie2 = {k: cookie[k] for k in cookie if k != "domain"}
                try:
                    driver.add_cookie(cookie2)
                except Exception:
                    pass
        driver.refresh()
        print(f"Cookies loaded for {chat_id}")
        return True
    except Exception as e:
        print(f"Не удалось загрузить cookies: {e}")
        return False

def save_localstorage_for_chat(driver, chat_id):
    try:
        data = driver.execute_script("return JSON.stringify(window.localStorage);")
        with open(localstorage_path_for_chat(chat_id), "w", encoding="utf-8") as f:
            f.write(data)
        print(f"LocalStorage saved for {chat_id}")
    except Exception as e:
        print(f"Не удалось сохранить localStorage: {e}")

def load_localstorage_for_chat(driver, chat_id):
    path = localstorage_path_for_chat(chat_id)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        driver.execute_script("window.localStorage.clear();")
        script = f"var items = {data}; for (var k in items) {{ window.localStorage.setItem(k, items[k]); }}"
        driver.execute_script(script)
        driver.refresh()
        print(f"LocalStorage loaded for {chat_id}")
        return True
    except Exception as e:
        print(f"Не удалось загрузить localStorage: {e}")
        return False

# ----------------------
# Selenium
# ----------------------
def start_driver_with_profile(chat_id=None, headless=HEADLESS):
    profile_dir = Path(PROFILES_DIR) / f"profile_{chat_id}"
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--incognito")
    options.add_argument("--window-position=0,0")
    options.add_argument("--window-size=1024,768")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=0")
    if headless:
        options.add_argument("--headless=new")

    driver = webdriver.Chrome(
        service=Service(CHROME_DRIVER_PATH),
        options=options
    )
    driver.set_page_load_timeout(30)
    return driver, str(profile_dir)

def safe_quit_session(session):
    driver = session.get("driver")
    tempdir = session.get("tempdir")

    try:
        if driver:
            driver.quit()
    except Exception:
        pass
    try:
        if tempdir and os.path.isdir(tempdir) and tempfile.gettempdir() in tempdir:
            shutil.rmtree(tempdir)
    except Exception:
        pass

def safe_quit_session_for_chat(chat_id):
    with SESSIONS_LOCK:
        if SESSIONS.get(chat_id) is not None:
            driver = SESSIONS.get(chat_id)["driver"]
            save_cookies_for_chat(driver, chat_id)
            save_localstorage_for_chat(driver, chat_id)
        session = SESSIONS.pop(chat_id, None)
    if session:
        safe_quit_session(session)

# ----------------------
# Обёртка next_step
# ----------------------
def set_next_step(chat_id, handler, step_name):
    """Устанавливает следующий шаг для пользователя."""
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if session:
            session["awaiting_step"] = step_name
    reset_activity_timer(chat_id)
    # register_next_step_handler_by_chat_id умеет работать без объекта Message
    bot.register_next_step_handler_by_chat_id(chat_id, handler)
def set_next_step_msg(chat_id, handler, step_name, msg):
    """Устанавливает следующий шаг для пользователя."""
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if session:
            session["awaiting_step"] = step_name
    reset_activity_timer(chat_id)
    # register_next_step_handler_by_chat_id умеет работать без объекта Message
    bot.register_next_step_handler(msg, handler)


# ----------------------
# Утилиты
# ----------------------
def is_logged_in(driver, timeout=5):
    try:
        wait = WebDriverWait(driver, timeout)
        wait.until(EC.presence_of_element_located((By.XPATH, "/html/body/div/div[1]/div[1]/form/div[4]/div[2]/div/div/input")))
        return False
    except Exception:
        return True
def is_busy(chat_id):
    with SESSIONS_LOCK:
        s = SESSIONS.get(chat_id)
        return bool(s and s.get("processing"))

# ----------------------
# Команды бота
# ----------------------
@bot.message_handler(commands=['setphone'])
def handle_setphone(message):
    chat_id = message.chat.id
    allowed, wait_sec = can_execute_command(chat_id, "setphone")
    if not allowed:
        bot.send_message(chat_id, f"⏳ Команды можно вызвать не чаще, чем раз в {int(COMMAND_COOLDOWN["setphone"]/60)} минуты. Подождите {wait_sec} секунд.")
        return
    if is_busy(chat_id):
        bot.send_message(chat_id, "⏳ Подождите, текущая операция ещё выполняется.")
        return

    with SESSIONS_LOCK:
        if not SESSIONS.get(chat_id):
            SESSIONS[chat_id] = {"driver": None, "awaiting_sms": False,
                                 "sms_event": None, "last_action_time": time.time(),
                                 "processing": True}
        else:
            SESSIONS[chat_id]["processing"] = True

    reset_activity_timer(chat_id)
    msg = bot.send_message(chat_id, "Введите новый номер телефона (в формате +7...):")
    set_next_step(chat_id, _receive_new_phone, "get_phone")


# ----------------------
# Выбор чата через кнопки
# ----------------------
def ask_chat_selection(driver, chat_id):
    time.sleep(4)
    try:
        chat_elements = driver.find_elements(By.CSS_SELECTOR, "div.item.svelte-rg2upy")
        markup = types.InlineKeyboardMarkup()

        # кнопка отмены
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{chat_id}"))

        chats_list = []
        for i, chat in enumerate(chat_elements):
            try:
                name_span = chat.find_element(By.CSS_SELECTOR,
                                              "h3.title.svelte-q2jdqb span.name.svelte-1riu5uh span.text.svelte-1riu5uh")
                chat_name = name_span.text.strip()
                if chat_name:
                    chats_list.append(chat_name)
                    # передаём только индекс
                    markup.add(types.InlineKeyboardButton(chat_name, callback_data=f"chat:{chat_id}:{i}"))
            except Exception:
                continue

        # сохраняем список чатов в сессии
        with SESSIONS_LOCK:
            if chat_id in SESSIONS:
                SESSIONS[chat_id]["chat_list"] = chats_list

        bot.send_message(chat_id, "Выберите чат:", reply_markup=markup)
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при загрузке списка чатов: {e}")
        safe_quit_session_for_chat(chat_id)
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if session:
            session["awaiting_step"] = "vibor2"
    reset_activity_timer(chat_id)


# ----------------------
# Обработка callback от кнопок
# ----------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("chat:") or call.data.startswith("cancel:"))
def handle_chat_selection(call):
    parts = call.data.split(":")
    action = parts[0]
    chat_id = int(parts[1])

    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session or not session.get("driver"):
            bot.answer_callback_query(call.id, "Сессия недоступна, начните заново.")
            return
        driver = session["driver"]

    if action == "cancel":
        bot.edit_message_text("Выбор чата отменён.", chat_id, call.message.message_id)
        safe_quit_session_for_chat(chat_id)
        return

    if action == "chat":
        try:
            idx = int(parts[2])
            chat_name = session.get("chat_list", [])[idx]
        except Exception:
            bot.answer_callback_query(call.id, "Ошибка: чат не найден.")
            return

        bot.edit_message_text(f"Вы выбрали чат: {chat_name}", chat_id, call.message.message_id)
        with SESSIONS_LOCK:
            session["selected_chat"] = chat_name
        ask_action_selection(chat_id)
# ======================== кнопки действий ========================
def ask_action_selection(chat_id):
    # если сессия ещё активна → показываем кнопки
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📖 Прочитать", callback_data=f"read:{chat_id}"))
    markup.add(types.InlineKeyboardButton("✍️ Написать", callback_data=f"write:{chat_id}"))
    markup.add(types.InlineKeyboardButton("🔄 Выбрать другой чат", callback_data=f"rechat:{chat_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel2:{chat_id}"))
    bot.send_message(chat_id, "Что вы хотите сделать?", reply_markup=markup)
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if session:
            session["awaiting_step"] = "vibor1"
    reset_activity_timer(chat_id)



# ======================== обработка кнопок ========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("read:") or call.data.startswith("write:") or call.data.startswith("cancel2:") or call.data.startswith("rechat:"))
def handle_action_selection(call):
    parts = call.data.split(":")
    action = parts[0]
    chat_id = int(parts[1])

    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session or not session.get("driver"):
            bot.answer_callback_query(call.id, "Сессия недоступна, начните заново.")
            return
        driver = session["driver"]
        chat_name = session.get("selected_chat")
    reset_activity_timer(chat_id)
    bot.edit_message_text(f"Выполняю...", chat_id, call.message.message_id)
    if action == "read":
        bot.answer_callback_query(call.id)
        _open_chat_and_fetch(driver, chat_id, chat_name)

    elif action == "write":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(chat_id, "Введите сообщение для отправки в чат:")
        set_next_step_msg(chat_id, _receive_message_to_send, "send_message", msg)

    elif action == "cancel2":
        safe_quit_session_for_chat(chat_id)
        bot.answer_callback_query(call.id, "Сессия закрыта.")
        bot.send_message(chat_id, "✅ Сессия завершена.")
    elif action == "rechat":
        bot.answer_callback_query(call.id)
        ask_chat_selection(driver, chat_id)


# ======================== отправка сообщения ========================
def image_to_clipboard(img: Image.Image):
    """Сохраняем PIL Image в буфер обмена Windows как DIB (для вставки Ctrl+V)"""
    output = BytesIO()
    img.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # Убираем заголовок BMP
    output.close()
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()

PENDING_MESSAGES = defaultdict(list)
BUFFERED_MESSAGES = defaultdict(list)

def _receive_message_to_send(message):
    chat_id = message.chat.id
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session or not session.get("driver"):
            bot.send_message(chat_id, "Сессия истекла. Запустите /checkmax заново.")
            return

    # сохраняем в буфер
    BUFFERED_MESSAGES[chat_id].append(message)
    reset_activity_timer(chat_id)

    # предлагаем пользователю либо дописать ещё, либо отправить
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Добавить ещё", callback_data=f"addmsg:{chat_id}"))
    markup.add(types.InlineKeyboardButton("📤 Отправить все", callback_data=f"sendall:{chat_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancelmsg:{chat_id}"))
    bot.send_message(chat_id, f"✅ Сообщение добавлено в очередь ({len(BUFFERED_MESSAGES[chat_id])} шт.)", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("addmsg:") or call.data.startswith("sendall:") or call.data.startswith("cancelmsg:"))
def handle_message_buffer(call):
    chat_id = int(call.data.split(":")[1])
    action = call.data.split(":")[0]
    bot.edit_message_text(f"Выполняю...", chat_id, call.message.message_id)
    if action == "addmsg":
        bot.answer_callback_query(call.id, "Напишите ещё сообщение:")
        msg = bot.send_message(chat_id, "Введите следующее сообщение:")
        set_next_step_msg(chat_id, _receive_message_to_send, "send_message", msg)

    elif action == "sendall":
        bot.answer_callback_query(call.id, "Отправляю все сообщения...")
        messages = BUFFERED_MESSAGES.pop(chat_id, [])
        if not messages:
            bot.send_message(chat_id, "Очередь пуста.")
            return
        _send_messages_to_chat(chat_id, messages)

    elif action == "cancelmsg":
        BUFFERED_MESSAGES.pop(chat_id, None)
        bot.answer_callback_query(call.id, "Очередь очищена.")
        bot.send_message(chat_id, "❌ Отправка отменена.")

def _send_messages_to_chat(chat_id, messages):
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session or not session.get("driver"):
            bot.send_message(chat_id, "Сессия истекла. Запустите /checkmax заново.")
            return
        driver = session["driver"]
        chat_name = session.get("selected_chat")

    try:
        if not choose_chat(driver, chat_name):
            bot.send_message(chat_id, f"Чат '{chat_name}' не найден.")
            return

        time.sleep(1.2)
        input_box = driver.find_element(By.XPATH, "/html/body/div[4]/div[1]/div[2]/main/div[3]/div/div[2]/div[3]/div/div/div[2]/div/div/div/div/div/div")
        send_btn = driver.find_element(By.XPATH, "/html/body/div[4]/div[1]/div[2]/main/div[3]/div/div[2]/div[3]/div/div/div[4]/button")

        for message in messages:
            if getattr(message, "media_group_id", None):
                PENDING_MESSAGES[(chat_id, message.media_group_id)].append(message)
                # Ждём, пока Telegram дольёт остальные фото в группу
                time.sleep(1)
                messages_to_send = PENDING_MESSAGES.pop((chat_id, message.media_group_id), [])
            else:
                # Обычное сообщение — просто одно
                messages_to_send = [message]

            if not choose_chat(driver, chat_name):
                bot.send_message(chat_id, f"Чат '{chat_name}' не найден.")
                return
            time.sleep(0.5)

            input_box.click()
            time.sleep(0.3)

            # Теперь проходим по всем сообщениям из пачки
            for msg in messages_to_send:
                media_files = []

                if msg.photo:
                    file_id = msg.photo[-1].file_id
                    file_info = bot.get_file(file_id)
                    media_files.append(file_info.file_path)

                if msg.document:
                    file_info = bot.get_file(msg.document.file_id)
                    media_files.append(file_info.file_path)

                # Текст
                if msg.text:
                    input_box.send_keys(Keys.CONTROL + "a")
                    input_box.send_keys(Keys.DELETE)
                    input_box.send_keys(msg.text.strip())
                    time.sleep(0.2)

                # Отправка медиа через Ctrl+V
                for file_path in media_files:
                    temp_file = os.path.join(tempfile.gettempdir(), os.path.basename(file_path))
                    try:
                        file_data = bot.download_file(file_path)
                        with open(temp_file, "wb") as f:
                            f.write(file_data)
                        img = Image.open(temp_file)
                    except Exception:
                        img = ImageGrab.grabclipboard()
                        if isinstance(img, list):
                            img = img[0]
                        if not img:
                            bot.send_message(chat_id, f"Файл {file_path} не найден и буфер пуст.")
                            continue

                    image_to_clipboard(img)

                    input_box.click()
                    time.sleep(0.2)
                    input_box.send_keys(Keys.CONTROL + "v")
                    time.sleep(3)

                    if os.path.exists(temp_file):
                        os.remove(temp_file)

                send_btn.click()

            # сюда же вставляется логика с фото/документами из твоего кода
            # (копирование в буфер и вставка через Ctrl+V)

        bot.send_message(chat_id, "✉️ Все сообщения отправлены!")
        ask_action_selection(chat_id)

    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при отправке: {e}")
        safe_quit_session_for_chat(chat_id)



def _receive_new_phone(message):
    chat_id = message.chat.id
    phone = message.text.strip()

    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session:
            SESSIONS[chat_id] = {"phone": phone, "driver": None, "tempdir": None}
            session = SESSIONS[chat_id]
        else:
            # Если есть активный драйвер — закрываем
            if session.get("driver"):
                try:
                    session["driver"].quit()
                except Exception:
                    pass
                session["driver"] = None

            # Удаляем старую папку профиля
            profile_dir = Path(PROFILES_DIR) / f"profile_{chat_id}"
            if profile_dir.exists():
                try:
                    import stat
                    def remove_readonly(func, path, excinfo):
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    shutil.rmtree(profile_dir, onerror=remove_readonly)
                    print(f"Удалён старый профиль для {chat_id}")
                except Exception as e:
                    print(f"Не удалось удалить старый профиль: {e}")
            cookie_path = cookie_path_for_chat(chat_id)
            if os.path.exists(cookie_path):
                os.remove(cookie_path)

            localstorage_path = localstorage_path_for_chat(chat_id)
            if os.path.exists(localstorage_path):
                os.remove(localstorage_path)

            session["phone"] = phone
            session["awaiting_step"] = None
    with SESSIONS_LOCK:
        if chat_id in SESSIONS:
            SESSIONS[chat_id]["processing"] = False

    save_phone_cache()
    bot.send_message(chat_id, f"Номер телефона обновлён: {phone}\nСтарые cookies и localStorage удалены. Следующая сессия будет чистой.")


# ----------------------
# /start
# ----------------------
@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Команды: \n /checkmax - проверяет MAX. \n /setphone - Устанавливает телефон по которому бот будет заходить в MAX.")

# ----------------------
# /checkmax
# ----------------------
@bot.message_handler(commands=['checkmax'])
def handle_checkmax(message):
    chat_id = message.chat.id
    allowed, wait_sec = can_execute_command(chat_id, "checkmax")
    if not allowed:
        bot.send_message(chat_id, f"⏳ Команды можно вызвать не чаще, чем раз в {int(COMMAND_COOLDOWN["checkmax"]/60)} минуты. Подождите {wait_sec} секунд.")
        return
    if is_busy(chat_id):
        bot.send_message(chat_id, "⏳ Подождите, текущая операция ещё выполняется.")
        return

    restore_phone_cache()
    reset_activity_timer(chat_id)

    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session:
            SESSIONS[chat_id] = {"processing": True}
        else:
            session["processing"] = True

        phone_number = session.get("phone") if session else None

    if not phone_number:
        bot.send_message(chat_id, "📱 Сначала задайте телефон через /setphone")
        with SESSIONS_LOCK:
            SESSIONS[chat_id]["processing"] = False
        return
    bot.send_message(chat_id, "Захожу в MAX...")
    threading.Thread(target=_start_login_flow, args=(chat_id, phone_number)).start()



def _receive_phone_and_start(message, parent_chat_id):
    phone = message.text.strip()
    reset_activity_timer(parent_chat_id)
    with SESSIONS_LOCK:
        SESSIONS[parent_chat_id] = {"phone": phone, "driver": None, "tempdir": None}
        session = SESSIONS.get(parent_chat_id)
        if session:
            session["awaiting_step"] = None
    save_phone_cache()
    bot.send_message(parent_chat_id, f"Принял номер {phone}. Запускаю вход...")
    t = threading.Thread(target=_start_login_flow, args=(parent_chat_id, phone))
    t.start()

# ----------------------
# Авторизация
# ----------------------
def _start_login_flow(chat_id, phone_number):
    try:
        driver, tempdir = start_driver_with_profile(chat_id)
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка запуска браузера: {e}")
        return

    with SESSIONS_LOCK:
        SESSIONS[chat_id] = {
            "driver": driver,
            "awaiting_sms": True,
            "sms_event": threading.Event(),
            "last_action_time": time.time(),
        }
    reset_activity_timer(chat_id)
    try:
        try:
            load_cookies_for_chat(driver, chat_id)
            load_localstorage_for_chat(driver, chat_id)
        except Exception:
            pass

        driver.get(BASE_URL)

        if is_logged_in(driver):
            bot.send_message(chat_id, "Сессия восстановления прошла — вы уже в системе.")
            ask_chat_selection(driver, chat_id)
            return

        wait = WebDriverWait(driver, 20)
        phone_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.field")))
        phone_input.clear()
        phone_input.send_keys(phone_number)

        login_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "/html/body/div/div[1]/div[1]/form/div[4]/button")))
        login_btn.click()
        msg = bot.send_message(chat_id, "Код СМС отправлен. Введите его сюда:")
        set_next_step_msg(chat_id, _receive_sms_code, "sms_code", msg)

    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при входе: {e}")
        safe_quit_session_for_chat(chat_id)

def _receive_sms_code(message):
    chat_id = message.chat.id
    code = message.text.strip()
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session or session.get("awaiting_step") != "sms_code":
            bot.send_message(chat_id, "Сессия истекла. Запустите /checkmax заново.")
            return
        session["awaiting_step"] = None
    if not session or not session.get("driver"):
        bot.send_message(chat_id, "Сессия не найдена — запустите /checkmax заново.")
        return
    driver = session["driver"]
    wait = WebDriverWait(driver, 20)
    reset_activity_timer(chat_id)
    try:
        inputs = wait.until(EC.presence_of_all_elements_located((By.XPATH, "/html/body/div/div[1]/div[1]/form/div[3]/div[2]/div/input")))
        for i, d in enumerate(code[:6]):
            if i < len(inputs):
                inputs[i].clear()
                inputs[i].send_keys(d)

        ask_chat_selection(driver, chat_id)

    except Exception as e:
        bot.send_message(chat_id, f"Ошибка входа: {e}")
        safe_quit_session_for_chat(chat_id)
    finally:
        with SESSIONS_LOCK:
            if chat_id in SESSIONS:
                SESSIONS[chat_id]["processing"] = False


# ----------------------
# Чат и сообщения
# ----------------------
def choose_chat(driver, chat_name):
    try:
        chat_elements = driver.find_elements(By.CSS_SELECTOR, "div.item.svelte-rg2upy")
        for chat in chat_elements:
            try:
                name_span = chat.find_element(By.CSS_SELECTOR, "h3.title.svelte-q2jdqb span.name.svelte-1riu5uh span.text.svelte-1riu5uh")
                if name_span.text.strip() == chat_name:
                    chat.click()
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        print(f"Ошибка при выборе чата: {e}")
        return False

def _open_chat_and_fetch(driver, chat_id, chat_name):
    with SESSIONS_LOCK:
        session = SESSIONS.get(chat_id)
        if not session:
            bot.send_message(chat_id, "Сессия истекла, начните заново с /checkmax.")
            return
        session["awaiting_step"] = None

    reset_activity_timer(chat_id)

    if not choose_chat(driver, chat_name):
        bot.send_message(chat_id, f"Чат '{chat_name}' не найден.")
        try:
            save_cookies_for_chat(driver, chat_id)
            save_localstorage_for_chat(driver, chat_id)
        except Exception:
            pass
        safe_quit_session(session)
        return

    time.sleep(2.5)
    messages_container = driver.find_element(
        By.XPATH,
        "/html/body/div[4]/div[1]/div[2]/main/div[3]/div/div[2]/div[2]/div[1]/div/div"
    )

    message_items = messages_container.find_elements(By.CSS_SELECTOR, "div.item.svelte-rg2upy")
    messages_with_index = []

    for msg in message_items:
        try:
            idx = int(msg.get_attribute("data-index") or -1)

            # Текст сообщения
            try:
                text_el = msg.find_element(By.CSS_SELECTOR, "span.text.svelte-1htnb3l")
                text = text_el.text.strip()
            except Exception:
                text = ""

            # Автор
            autor = chat_name
            try:
                autor_el = msg.find_element(By.CSS_SELECTOR, "span.text.svelte-1riu5uh")
                autor = autor_el.text.strip()
            except:
                try:
                    msg.find_element(By.CSS_SELECTOR, "div.indicators.svelte-13lobfv use")
                    autor = "Вы"
                except:
                    pass

            messages_with_index.append((idx, {"text": f"{autor}: {text}", "element": msg}))
        except Exception:
            continue

    messages_with_index.sort(key=lambda x: x[0])
    last_10 = messages_with_index[-10:]

    bot.send_message(chat_id, f"Найдено {len(last_10)} сообщений. Последние {len(last_10)}:")

    for _, msg_data in last_10:
        msg_element = msg_data["element"]
        final_text = msg_data["text"]

        # Файлы — только название
        try:
            file_elements = msg_element.find_elements(By.CSS_SELECTOR, "div.title.svelte-1cw64r4")
            file_texts = []
            for fe in file_elements:
                file_name = fe.text.strip()
                if file_name:
                    file_texts.append(f"<b>Файл:</b> {file_name}")
            if file_texts:
                final_text += "\n" + "\n".join(file_texts)
        except Exception as e:
            print(f"Ошибка обработки файлов: {e}")

        # Картинки — отправляем все без проверки
        try:
            img_elements = msg_element.find_elements(By.CSS_SELECTOR, "img.image.svelte-1aizpza")
            img_srcs = [img.get_attribute("src") for img in img_elements if not img.get_attribute("alt")]
        except Exception as e:
            print(f"Ошибка обработки картинок: {e}")
            img_srcs = []

        # Отправка текста
        if final_text.strip():
            bot.send_message(chat_id, final_text, parse_mode="HTML")

        # Отправка картинок
        try:
            for src in img_srcs:
                file_name = os.path.basename(src.split("?")[0])
                file_path = os.path.join(tempfile.gettempdir(), file_name)
                r = requests.get(src, stream=True, timeout=10)
                r.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)

                with open(file_path, "rb") as f:
                    bot.send_photo(chat_id, f)

                os.remove(file_path)
        except Exception as e:
            print(f"Ошибка при скачивании/отправке картинки: {e}")

    try:
        save_cookies_for_chat(driver, chat_id)
        save_localstorage_for_chat(driver, chat_id)
    except Exception:
        pass

    with SESSIONS_LOCK:
        if chat_id in SESSIONS:
            SESSIONS[chat_id]["processing"] = False

    ask_action_selection(chat_id)



# ----------------------
# Запуск бота
# ----------------------
if __name__ == "__main__":
    restore_phone_cache()
    print("Bot started...")
    bot.infinity_polling(timeout=60, long_polling_timeout=5)
