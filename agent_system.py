# -*- coding: utf-8 -*-
"""
Центральный диспетчер смены мультиагентной робототехнической системы.
Разработано для соревнований «Кубок Губернатора по искусственному интеллекту».

Основные задачи модуля:
1. Захват и офлайн-распознавание голосовых команд через библиотеку Vosk и микрофон.
2. Обработка текстовых команд оператора из интерактивной консоли.
3. Парсинг естественного языка (русский язык): выделение целевого робота,
   проверка безопасности команд, разбор составных многошаговых маршрутов и длительностей.
4. TCP-сервер координации: поддержание постоянного сопряжения с роботами
   (EyeCar, PuppyPi, Rover), мониторинг дисконнектов и мгновенная рассылка команд.
5. Голосовая обратная связь (TTS) и подробное журналирование всех событий смены в shift_log.txt.
"""

import os
import sys
import time
import json
import queue
import socket
import datetime
import threading
import sounddevice as sd
from vosk import Model, KaldiRecognizer, SetLogLevel

# Отключаем отладочный вывод Kaldi/Vosk, чтобы не засорять терминал оператора
SetLogLevel(-1)

# Принудительная настройка вывода UTF-8 с немедленным сбросом буфера
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

# ---------------------------------------------------------------------------
# Модуль голосового оповещения (Text-to-Speech)
# ---------------------------------------------------------------------------
# Сначала пробуем использовать стандартный COM-интерфейс Windows SAPI (самый быстрый отклик).
# Если pywin32 недоступен, в качестве запасного варианта вызываем System.Speech через PowerShell.
speaker = None
try:
    import win32com.client
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
except Exception:
    speaker = None

def speak(text):
    """
    Озвучивает короткую статусную фразу оператору (асинхронно, не блокируя поток управления).
    """
    if speaker is not None:
        try:
            # Флаг 1 означает асинхронное воспроизведение SAPI
            speaker.Speak(text, 1)
            return
        except Exception:
            pass
    try:
        # Резервный вызов синтезатора через фоновый PowerShell
        clean = text.replace('"', ' ').replace("'", " ")
        ps = f'Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).SpeakAsync("{clean}")'
        import subprocess
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Журналирование смены
# ---------------------------------------------------------------------------
def write_log(tag, message):
    """
    Записывает событие смены в файл shift_log.txt с точной временной меткой.
    Используется для аудита действий и разбора инцидентов во время заездов.
    """
    time_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        with open("shift_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{time_str}] [{tag}] {message}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Сетевые утилиты и состояние подключений
# ---------------------------------------------------------------------------
def get_ip():
    """
    Определяет рабочий IP-адрес текущего компьютера в локальной сети.
    Используется трюк с фиктивным UDP-подключением (без отправки трафика),
    чтобы ОС сама вернула адрес используемого сетевого интерфейса.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.connect(('8.8.8.8', 80))
        res = s.getsockname()[0]
    except Exception:
        try:
            res = socket.gethostbyname(socket.gethostname())
        except Exception:
            res = '127.0.0.1'
    finally:
        s.close()
    return res

# Словарь подключенных роботов вида: {"eyecar": socket, "puppypi": socket, "rover": socket}
connected_robots = {}

# ---------------------------------------------------------------------------
# Лингвистический анализатор (парсер естественной речи оператора)
# ---------------------------------------------------------------------------
def parse_order(text):
    """
    Анализирует текст команды (распознанный голос или ввод из консоли).
    
    Возвращает кортеж: (успех: bool, сообщение_или_статус: str, план_действий: list)
    Элемент плана: (имя_робота, название_навыка, длительность_в_секундах)
    """
    text = text.lower().strip()
    if not text:
        return False, "Пустая команда", []

    # 1. Проверка на экстренную остановку (наивысший приоритет безопасности)
    stop_tokens = [
        "стоп", "стой", "стоять", "стойте", "остановись", "остановитесь", "остановить",
        "тормоз", "тормози", "тормозите", "всем стоп", "все стоп", "стоп всем", "все стоять",
        "всем стоять", "отмена", "хватит", "замри", "останови всех", "остановить всех"
    ]
    motion_tokens = [
        "вперед", "вперёд", "назад", "налево", "направо", "круг",
        "сядь", "присядь", "ляг", "лечь", "встань", "лап", "потянись", "поклонись"
    ]
    has_stop = any(token in text for token in stop_tokens)
    has_motion = any(m in text for m in motion_tokens)

    # Если звучит стоп-слово без сопутствующей команды движения — мгновенный тормоз
    if has_stop and not has_motion:
        has_eyecar = any(w in text for w in ["икар", "айкар", "ай кар", "eyecar", "машинк", "машин", "курьер"])
        has_puppy  = any(w in text for w in ["собак", "пёс", "щенок", "папи", "puppypi"])
        has_rover  = any(w in text for w in ["ровер", "марсоход", "вездеход", "rover"])

        if has_eyecar and not (has_puppy or has_rover):
            return True, "Остановка Икара", [("eyecar", "стоп", 0)]
        elif has_puppy and not (has_eyecar or has_rover):
            return True, "Остановка Собаки", [("puppypi", "стоп", 0)]
        elif has_rover and not (has_eyecar or has_puppy):
            return True, "Остановка Ровера", [("rover", "стоп", 0)]
        else:
            # Если адресат не уточнен или сказано "всем стоп" — останавливаем сразу всех
            return True, "Остановка всех роботов", [("all", "стоп", 0)]

    # 2. Фильтрация заведомо невыполнимых и опасных команд (safety guardrails)
    for bad in ["сальто", "кульбит", "прыг", "прыжок", "лети", "взлети", "плыви", "ныряй"]:
        if bad in text:
            return False, "Отказ: навык запрещён", []

    # 3. Проверка на неопределенность направления
    if "туда" in text or "куда-то" in text:
        return False, "Уточните направление", []

    # 4. Определение адресатов в тексте
    has_eyecar = any(w in text for w in ["икар", "айкар", "ай кар", "eyecar", "машинк", "машин", "курьер"])
    has_puppy  = any(w in text for w in ["собак", "пёс", "щенок", "папи", "puppypi"])
    has_rover  = any(w in text for w in ["ровер", "марсоход", "вездеход", "rover"])
    has_anyone = any(w in text for w in ["кто-нибудь", "кто нибудь", "кто-то", "кто то", "кому-нибудь", "кому нибудь"])

    # Манёвр «круг» поддерживают колёсные платформы (Икар или Ровер)
    if "круг" in text and not (has_eyecar or has_rover):
        return False, "Уточните: икар или ровер", []

    # Уникальные навыки четвероногого робота PuppyPi
    dog_unique = ["лап", "сядь", "присядь", "сидеть", "ляг", "лечь", "лежать", "поклон", "поклонись", "потянись", "тянись", "встань", "вставай"]
    if any(sk in text for sk in dog_unique) and not (has_eyecar or has_rover):
        has_puppy = True

    # Защита от неопределенного исполнителя для общих команд движения
    if has_anyone and any(m in text for m in ["вперед", "вперёд", "назад", "налево", "направо"]):
        return False, "Уточните: икар, собака или ровер", []

    if not (has_eyecar or has_puppy or has_rover):
        return False, "Не указан робот!", []

    # 5. Разбор составных инструкций через союзы очередности ("затем", "потом", "и")
    cleaned = text.replace(" затем ", ", ").replace(" потом ", ", ").replace(" и ", ", ")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]

    default_robot = "eyecar" if has_eyecar else ("puppypi" if has_puppy else "rover")
    plan = []
    current_robot = default_robot

    for part in parts:
        # Переключение контекста робота внутри подкоманды, если назван другой
        if any(w in part for w in ["собак", "пёс", "щенок", "папи", "puppypi"]):
            current_robot = "puppypi"
        elif any(w in part for w in ["икар", "айкар", "ай кар", "eyecar", "машинк", "машин", "курьер"]):
            current_robot = "eyecar"
        elif any(w in part for w in ["ровер", "вездеход", "rover"]):
            current_robot = "rover"
        elif any(sk in part for sk in dog_unique):
            current_robot = "puppypi"

        words = part.split()
        # Пропускаем изолированные упоминания имен роботов без действий
        if len(words) == 1 and any(words[0].startswith(rw) for rw in ["икар", "айкар", "машинк", "собак", "ровер", "кто"]):
            continue

        # Поиск длительности в секундах, если указано число
        seconds = None
        for w in words:
            if w.isdigit():
                seconds = float(w)
                break

        # Разбор навыков для четвероногого робота (PuppyPi)
        if current_robot == "puppypi":
            if "лап" in part:
                plan.append(("puppypi", "дать лапу", 2.0))
            elif any(w in part for w in ["сядь", "присядь", "сидеть"]):
                plan.append(("puppypi", "сесть", 2.0))
            elif any(w in part for w in ["ляг", "лечь", "лежать"]):
                plan.append(("puppypi", "лечь", 2.0))
            elif any(w in part for w in ["встань", "вставай"]):
                plan.append(("puppypi", "встать", 2.0))
            elif any(w in part for w in ["потянись", "поклонись", "поклон", "тянись"]):
                plan.append(("puppypi", "потянуться", 2.0))
            elif any(w in part for w in ["вперед", "вперёд", "пройди", "иди"]):
                dur = seconds if seconds is not None else 3.0
                plan.append(("puppypi", "идти вперёд", dur))
            elif "назад" in part:
                dur = seconds if seconds is not None else 3.0
                plan.append(("puppypi", "идти назад", dur))
            elif any(w in part for w in ["налево", "влево", "лево"]):
                dur = seconds if seconds is not None else 3.0
                plan.append(("puppypi", "налево", dur))
            elif any(w in part for w in ["направо", "вправо", "право"]):
                dur = seconds if seconds is not None else 3.0
                plan.append(("puppypi", "направо", dur))
            elif any(w in part for w in ["стоп", "стой", "остановись"]):
                plan.append(("puppypi", "стоп", 0))
            else:
                return False, "Отказ: нет такого навыка", []

        # Разбор навыков для колёсных платформ (EyeCar / Rover)
        else:
            if "круг" in part:
                # Полный оборот для ровера быстрее (~6с), для машинки чуть дольше (~7с)
                dur = seconds if seconds is not None else (6.0 if current_robot == "rover" else 7.0)
                plan.append((current_robot, "круг", dur))
            elif any(w in part for w in ["вперед", "вперёд", "проедь", "езжай", "газ", "прямо"]):
                dur = seconds if seconds is not None else 3.0
                plan.append((current_robot, "ехать вперёд", dur))
            elif any(w in part for w in ["назад", "сдай", "реверс"]):
                dur = seconds if seconds is not None else 3.0
                plan.append((current_robot, "назад", dur))
            elif any(w in part for w in ["налево", "влево", "лево"]):
                dur = seconds if seconds is not None else 3.0
                plan.append((current_robot, "налево", dur))
            elif any(w in part for w in ["направо", "вправо", "право"]):
                dur = seconds if seconds is not None else 3.0
                plan.append((current_robot, "направо", dur))
            elif any(w in part for w in ["стоп", "стой", "остановись"]):
                plan.append((current_robot, "стоп", 0))
            else:
                return False, "Отказ: нет такого навыка", []

    if not plan:
        return False, "Неизвестная команда", []

    return True, "План готов", plan

# ---------------------------------------------------------------------------
# Отправка команд роботам по TCP
# ---------------------------------------------------------------------------
def send_command(robot, skill, duration):
    """
    Транслирует семантическое имя навыка в компактный сетевой пакет протокола
    вида '<ACTION> <DURATION>\\n' и отправляет на сокет конкретного робота.
    """
    cmd_map = {
        "идти вперёд": "FORWARD",
        "ехать вперёд": "FORWARD",
        "идти назад":  "BACKWARD",
        "назад":       "BACKWARD",
        "налево":      "LEFT",
        "направо":     "RIGHT",
        "круг":        "CIRCLE",
        "сесть":       "SIT",
        "лечь":        "LIE",
        "встать":      "STAND",
        "дать лапу":   "PAW",
        "потянуться":  "STRETCH",
        "стоп":        "STOP"
    }
    action = cmd_map.get(skill, "STOP")
    packet = f"{action} {duration}\n".encode('utf-8')

    # Широковещательный стоп всем активным роботам
    if robot == "all":
        for r_name, sock in list(connected_robots.items()):
            try:
                sock.sendall(b"STOP 0\n")
            except Exception:
                pass
        return True

    disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер"}
    r_disp = disp.get(robot, robot)

    # Проверяем, на связи ли запрошенный робот
    if robot not in connected_robots:
        print(f"[!] СБОЙ: Робот '{r_disp}' недоступен (не подключен)!", flush=True)
        write_log("СБОЙ", f"Команда '{skill}' не отправлена: {r_disp} не подключен")
        speak(f"{r_disp} не на связи")
        return False

    try:
        connected_robots[robot].sendall(packet)
        return True
    except Exception:
        # При сбое отправки фиксируем обрыв связи и удаляем робота из списка активных
        print(f"[!] СБОЙ: Потеря связи с '{r_disp}' при отправке!", flush=True)
        write_log("СБОЙ", f"Потеря связи с {r_disp} при отправке команды '{skill}'")
        speak(f"Потеря связи с {r_disp}")
        connected_robots.pop(robot, None)
        return False

# Событие отмены текущего выполняющегося плана при экстренной остановке
current_plan_event = threading.Event()

# ---------------------------------------------------------------------------
# Исполнитель плана (Sequential Execution Engine)
# ---------------------------------------------------------------------------
def execute_plan(plan, cancel_evt):
    """
    Пошагово выполняет составной план действий.
    
    Важная деталь безопасности: вместо time.sleep(duration) используется
    метод cancel_evt.wait(duration). Если во время движения поступит команда СТОП,
    событие мгновенно сработает, и программа прервет выполнение плана без задержек.
    """
    total = len(plan)
    for i, (rob, skill, dur) in enumerate(plan, 1):
        if cancel_evt.is_set():
            return
        disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер", "all": "Все"}
        rob_disp = disp.get(rob, rob)

        print(f"-> [ШАГ {i}/{total}] {rob_disp}: {skill} ({dur}с)...", flush=True)
        write_log("ШАГ", f"Шаг {i}/{total}: {rob_disp} -> {skill} ({dur} сек)")

        ok = send_command(rob, skill, dur)
        if not ok and rob != "all":
            write_log("СБОЙ", f"Шаг {i}/{total} пропущен: {rob_disp} не на связи")
            time.sleep(0.5)
            continue

        if dur > 0:
            # Ожидание выполнения маневра с возможностью немедленного прерывания
            if cancel_evt.wait(dur):
                return
            # Небольшая пауза стабилизации между соседними шагами плана
            if cancel_evt.wait(0.2):
                return

        write_log("ВЫПОЛНЕНО", f"Шаг {i}/{total} завершён ({rob_disp}: {skill})")
        print(f"-> [OK] Шаг {i}/{total} завершён", flush=True)

    if not cancel_evt.is_set():
        print("-> [OK] Все шаги плана выполнены!\n", flush=True)
        write_log("ВЫПОЛНЕНО", "Все шаги плана успешно выполнены")
        speak("Выполнено")

# ---------------------------------------------------------------------------
# Главный обработчик входящих распоряжений
# ---------------------------------------------------------------------------
def process_order(text, source="ТЕКСТ"):
    """
    Точка входа для распоряжения из любого источника (голос / клавиатура).
    """
    global current_plan_event
    write_log("РАСПОРЯЖЕНИЕ", f"[{source}] \"{text}\"")

    ok, message, plan = parse_order(text)

    # Если команда некорректна или отклонена правилами безопасности
    if not ok:
        print(f"\n[!] {message}", flush=True)
        write_log("ОТКАЗ", f"\"{text}\" -> {message}")
        speak(message)
        return

    # Обработка экстренной остановки
    if plan and any(skill == "стоп" for _, skill, _ in plan):
        current_plan_event.set()  # Отменяем текущий выполняемый план
        for rob, skill, dur in plan:
            send_command(rob, skill, 0)

        if any(rob == "all" for rob, _, _ in plan):
            print(f"\n[{source}]: \"{text}\" -> [!] ВСЕ РОБОТЫ ОСТАНОВЛЕНЫ\n", flush=True)
            write_log("СТОП", f"Экстренная остановка всех роботов по распоряжению \"{text}\"")
        else:
            disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер"}
            target = disp.get(plan[0][0], plan[0][0])
            print(f"\n[{source}]: \"{text}\" -> [!] {target} остановлен\n", flush=True)
            write_log("СТОП", f"Остановка робота {target}")
        speak("Стоп")
        return

    # Запуск нового плана
    disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер", "all": "Все"}
    desc = ", ".join([f"{disp.get(r, r)}: {s} ({d}с)" for r, s, d in plan])
    print(f"\n[{source}]: \"{text}\" -> {desc}", flush=True)
    write_log("ПЛАН", f"Сформирован план ({len(plan)} ш.): {desc}")

    # Сбрасываем старое событие и запускаем выполнение плана в фоновом потоке
    current_plan_event.set()
    current_plan_event = threading.Event()
    threading.Thread(target=execute_plan, args=(plan, current_plan_event), daemon=True).start()

# ---------------------------------------------------------------------------
# Мониторинг здоровья соединений (Watchdog)
# ---------------------------------------------------------------------------
def client_monitor(name, conn, addr):
    """
    Фоновый поток наблюдения за сокетом робота.
    Если робот выключается или теряет сеть, сокет сигнализирует обрыв (0 байт),
    и диспетчер немедленно уведомляет оператора голосом и делает запись в журнал.
    """
    while True:
        try:
            data = conn.recv(128)
            if not data:
                break
        except Exception:
            break

    # Фиксация потери связи
    if connected_robots.get(name) == conn:
        connected_robots.pop(name, None)
        disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер"}
        r_disp = disp.get(name, name)

        print(f"\n[!] СБОЙ: Робот '{r_disp}' ({addr[0]}) отключился!", flush=True)
        print("> ", end="", flush=True)

        write_log("СБОЙ", f"Робот '{r_disp}' ({addr[0]}) отключился от сервера")
        speak(f"{r_disp} отключился")

# ---------------------------------------------------------------------------
# TCP Сервер диспетчера
# ---------------------------------------------------------------------------
def server_loop(port=9090):
    """
    Слушает входящие TCP-подключения от клиентов на роботах.
    Ожидает приветственный пакет формата 'HELLO <robot_name>'.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', port))
    server.listen(5)

    disp = {"eyecar": "Икар", "puppypi": "Собака", "rover": "Ровер"}

    while True:
        try:
            conn, addr = server.accept()
            # Читаем приветственное рукопожатие
            hello = conn.recv(128).decode('utf-8', errors='ignore').strip()
            name = hello.split()[-1].lower() if hello else f"robot_{addr[0]}"

            # Если этот робот уже был подключен ранее, закрываем старый сокет
            if name in connected_robots:
                try:
                    connected_robots[name].close()
                except Exception:
                    pass

            connected_robots[name] = conn
            r_disp = disp.get(name, name)
            print(f"\n[+] Подключен робот: {r_disp} ({addr[0]})", flush=True)
            print("[OK] Сопряжение успешно! Можно говорить команды.\n> ", end="", flush=True)
            write_log("СЕТЬ", f"Подключен {r_disp} ({addr[0]})")

            # Запускаем персональный поток слежения за соединением этого робота
            threading.Thread(target=client_monitor, args=(name, conn, addr), daemon=True).start()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Поток распознавания речи (Vosk Speech-to-Text)
# ---------------------------------------------------------------------------
def voice_loop():
    """
    Непрерывно читает поток с микрофона и передает аудиоданные в модель Vosk.
    Работает локально без интернета.
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        model_path = os.path.join(base_dir, "model")
        # Загружаем локальную модель из папки model/ или системный кэш Vosk
        model = Model(model_path) if os.path.exists(model_path) else Model(lang="ru")
        rec = KaldiRecognizer(model, 16000)
        audio_queue = queue.Queue()

        def callback(indata, frames, time_info, status):
            audio_queue.put(bytes(indata))

        # Захват аудио: 16 кГц, моно, 16-битный PCM
        with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=callback):
            while True:
                data = audio_queue.get()
                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = res.get("text", "").strip()
                    if text:
                        process_order(text, source="ГОЛОС")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Главная функция запуска
# ---------------------------------------------------------------------------
def main():
    my_ip = get_ip()
    port = 9090

    print("=" * 55, flush=True)
    print(f"  IP диспетчера: {my_ip}", flush=True)
    print(f"  Порт:          {port}", flush=True)
    print("=" * 55, flush=True)
    print("Ожидаю подключения роботов (Икар, Собака, Ровер)...\n", flush=True)
    write_log("СТАРТ", f"Диспетчер смены запущен (IP: {my_ip}, порт: {port})")

    # Фоновые потоки: TCP-сервер и обработчик микрофона
    threading.Thread(target=server_loop, args=(port,), daemon=True).start()
    threading.Thread(target=voice_loop, daemon=True).start()

    # Голосовой сигнал готовности системы к приему команд
    speak("Готов")

    # Консольный интерактивный цикл ввода команд оператором
    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue
            if line.lower() in ["q", "exit", "выход"]:
                break
            process_order(line, source="ТЕКСТ")
        except (KeyboardInterrupt, EOFError):
            break

if __name__ == "__main__":
    main()
