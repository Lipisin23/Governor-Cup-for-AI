# -*- coding: utf-8 -*-
"""
Автономная голосовая и ручная станция управления (сервер потоковой телеметрии).
Используется для прямого пилотирования машинки EyeCar в реальном времени.

Ключевые отличия от agent_system:
- Потоковый протокол (heartbeat/streaming): сервер каждые 150 мс непрерывно
  отправляет текущий срез состояния платформы ('РЕЖИМ СКОРОСТЬ УГОЛ\\n').
- Поддерживает прямое ручное управление клавишами WASD и регулировку скорости (+/-).
- Офлайн-распознавание речи Vosk для динамических команд руления («влево», «быстрее», «стоп»).
- Поддержка макрокоманд автономных траекторий («склад», «гараж»).
"""

import os
import sys
import time
import json
import queue
import socket
import select
import threading
import sounddevice as sd
from vosk import Model, KaldiRecognizer

# Настройка UTF-8 для корректного отображения кириллицы
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

def find_ip():
    """
    Определяет локальный IP-адрес хоста для подсказки оператору.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        addr = s.getsockname()[0]
    except Exception:
        addr = '127.0.0.1'
    finally:
        s.close()
    return addr

def load_speech_model():
    """
    Ищет локальную модель Vosk в папке model/, затем в пользовательском кэше,
    либо инициирует загрузку стандартной легкой модели для русского языка.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(base_dir, "model")
    if os.path.exists(local_path):
        return Model(local_path)
    cache_path = os.path.expanduser(r"~\.cache\vosk\vosk-model-small-ru-0.22")
    if os.path.exists(cache_path):
        return Model(cache_path)
    return Model(lang="ru")

class VoiceStation:
    """
    Управляющая станция: объединяет аудио-поток с микрофона,
    сетевую передачу телеметрии и клавиатурный ввод.
    """
    def __init__(self, port=9090):
        self.port = port
        self.ip = find_ip()
        self.audio_q = queue.Queue()
        self.running = True
        
        # Текущее телеметрическое состояние платформы
        self.mode = "STOP"       # Режим: FORWARD, BACKWARD, LEFT, RIGHT, STOP, SKLAD, GARAZH
        self.speed = 1500        # Ширина импульса газа (1500 — нейтраль)
        self.angle = 90          # Угол колес (90 — прямо)
        self.last_text = "—"     # Последняя распознанная фраза
        
        self.client_socket = None
        self.client_addr = None
        self.lock = threading.Lock()

    def set_action(self, mode, speed=1500, angle=90, text=""):
        """
        Потокобезопасное обновление текущего состояния телеметрии.
        """
        with self.lock:
            self.mode = mode
            self.speed = speed
            self.angle = angle
            if text:
                self.last_text = text

    def get_state_line(self):
        """
        Формирует строку протокола для передачи роботу.
        """
        with self.lock:
            return f"{self.mode} {self.speed} {self.angle}\n"

    def parse_phrase(self, raw):
        """
        Парсер голосовых команд для оперативного руления.
        Возвращает: (режим, скорость, угол, текстовое описание)
        """
        text = raw.lower().strip()
        if not text:
            return None

        # Остановка
        if any(w in text for w in ["стоп", "стой", "тормоз", "остановись", "хватит", "замри"]):
            return "STOP", 1500, 90, "Полная остановка"

        # Движение вперед
        if any(w in text for w in ["вперед", "вперёд", "поехали", "газ", "двигайся", "едь", "езжай"]):
            return "FORWARD", 1570, 90, "Движение вперёд"

        # Движение назад
        if any(w in text for w in ["назад", "сдай", "реверс", "задний"]):
            return "BACKWARD", 1430, 90, "Задний ход"

        # Повороты
        if any(w in text for w in ["влево", "налево", "левее", "лево"]):
            spd = 1560 if self.mode != "BACKWARD" else 1440
            return "LEFT", spd, 75, "Поворот налево"

        if any(w in text for w in ["вправо", "направо", "правее", "право"]):
            spd = 1560 if self.mode != "BACKWARD" else 1440
            return "RIGHT", spd, 105, "Поворот направо"

        # Выравнивание колес
        if any(w in text for w in ["прямо", "ровно", "выровняй", "центр"]):
            spd = 1570 if self.mode in ["FORWARD", "LEFT", "RIGHT"] else (1430 if self.mode == "BACKWARD" else 1500)
            return ("FORWARD" if spd > 1500 else ("BACKWARD" if spd < 1500 else "STOP")), spd, 90, "Колёса прямо"

        # Плавная регулировка скорости
        if any(w in text for w in ["быстрее", "прибавь", "газу", "быстрей"]):
            if self.mode == "FORWARD":
                new_spd = min(1640, self.speed + 25)
                return "FORWARD", new_spd, self.angle, f"Ускорение ({new_spd})"
            elif self.mode == "BACKWARD":
                new_spd = max(1360, self.speed - 25)
                return "BACKWARD", new_spd, self.angle, f"Ускорение назад ({new_spd})"
            return None

        if any(w in text for w in ["тише", "медленнее", "сбавь", "потише"]):
            if self.mode == "FORWARD":
                new_spd = max(1520, self.speed - 25)
                return "FORWARD", new_spd, self.angle, f"Замедление ({new_spd})"
            elif self.mode == "BACKWARD":
                new_spd = min(1480, self.speed + 25)
                return "BACKWARD", new_spd, self.angle, f"Замедление ({new_spd})"
            return None

        # Автономные маршруты
        if "склад" in text:
            return "SKLAD", 1500, 90, "Маршрут: Склад"

        if "гараж" in text:
            return "GARAZH", 1500, 90, "Маршрут: Гараж"

        return None

    def audio_callback(self, indata, frames, time_info, status):
        """
        Коллбек аудио-потока: помещает сырые PCM-байты в очередь.
        """
        if status:
            pass
        self.audio_q.put(bytes(indata))

    def listen_loop(self, model):
        """
        Фоновый цикл распознавания речи из очереди аудиоданных.
        """
        rec = KaldiRecognizer(model, 16000)
        rec.SetWords(False)
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype='int16', channels=1, callback=self.audio_callback):
                while self.running:
                    try:
                        data = self.audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    if rec.AcceptWaveform(data):
                        res = json.loads(rec.Result())
                        phrase = res.get("text", "").strip()
                        if phrase:
                            parsed = self.parse_phrase(phrase)
                            if parsed:
                                m, spd, ang, desc = parsed
                                self.set_action(m, spd, ang, phrase)
                                print(f"\n[Голос]  Услышано: \"{phrase}\"")
                                print(f"[Робот]  -> {desc}  |  Режим: {m}  Газ: {spd}  Руль: {ang}°")
                            else:
                                print(f"\n[Голос]  Фраза: \"{phrase}\"  (команда не распознана)")
        except Exception as err:
            print(f"\n[Аудио]  Ошибка микрофона: {err}")

    def network_loop(self):
        """
        Сетевой цикл: ожидает подключение робота и передает поток телеметрии
        с постоянной частотой ~6-7 Гц (каждые 150 мс).
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        srv.listen(1)
        srv.settimeout(0.5)

        while self.running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            self.client_socket = conn
            self.client_addr = addr
            print(f"\n[Сеть]   Робот успешно подключился! IP: {addr[0]}:{addr[1]}")
            print(f"[Сеть]   Начинаю отправку команд в реальном времени...")

            conn.settimeout(0.5)
            last_sent = 0
            while self.running:
                now = time.time()
                # Регулярный heartbeat-пакет
                if now - last_sent >= 0.15:
                    msg = self.get_state_line()
                    try:
                        conn.sendall(msg.encode('utf-8'))
                        last_sent = now
                    except Exception:
                        print(f"\n[Сеть]   Робот {addr[0]} отключился.")
                        break

                # Проверка наличия входящих сообщений от робота
                r, _, _ = select.select([conn], [], [], 0.01)
                if r:
                    try:
                        incoming = conn.recv(128)
                        if not incoming:
                            print(f"\n[Сеть]   Робот {addr[0]} закрыл соединение.")
                            break
                    except Exception:
                        break

            try:
                conn.close()
            except Exception:
                pass
            self.client_socket = None
            self.client_addr = None
            print(f"[Сеть]   Жду новое подключение робота на порту {self.port}...")

        try:
            srv.close()
        except Exception:
            pass

    def run(self):
        """
        Запуск станции: стартует фоновые потоки и переходит в режим ожидания команд с клавиатуры.
        """
        print("=" * 60)
        print("         ГОЛОСОВОЙ ПУЛЬТ УПРАВЛЕНИЯ РОБОТОМ")
        print("=" * 60)
        print(f"  • Локальный IP ноутбука : {self.ip}")
        print(f"  • Порт для робота       : {self.port}")
        print("  • Модель распознавания  : Vosk (нейросеть офлайн, русский язык)")
        print("-" * 60)
        print("  Доступные голосовые команды:")
        print("    'вперёд', 'газ', 'поехали'    -> ехать прямо")
        print("    'назад', 'сдай назад'         -> ехать назад")
        print("    'влево', 'налево'             -> поворот влево")
        print("    'вправо', 'направо'           -> поворот вправо")
        print("    'прямо', 'выровняй'           -> колеса прямо")
        print("    'стоп', 'стой', 'тормози'     -> полная остановка")
        print("    'быстрее' / 'тише'            -> регулировка скорости")
        print("    'склад' / 'гараж'             -> доставка в зону")
        print("-" * 60)
        print("  Также можно вводить команды вручную прямо здесь:")
        print("    w / вперед, s / назад, a / влево, d / вправо, space / стоп, q / выход")
        print("=" * 60)

        print("\n[Старт]  Загрузка нейросетевой модели речи...")
        model = load_speech_model()
        print("[Старт]  Модель готова! Включаю микрофон...")

        t_mic = threading.Thread(target=self.listen_loop, args=(model,), daemon=True)
        t_mic.start()

        t_net = threading.Thread(target=self.network_loop, daemon=True)
        t_net.start()

        print(f"[Старт]  Сервер слушает микрофон и ждет робота на {self.ip}:{self.port}\n")

        try:
            # Клавиатурное руление оператором
            while self.running:
                cmd = input().strip().lower()
                if not cmd:
                    continue
                if cmd in ["q", "exit", "quit", "выход"]:
                    print("\n[Выход]  Завершение работы сервера...")
                    break
                elif cmd in ["w", "вперед", "вперёд"]:
                    self.set_action("FORWARD", 1570, 90, "Клавиатура: Вперёд")
                    print("[Ручной] -> ВПЕРЁД")
                elif cmd in ["s", "назад"]:
                    self.set_action("BACKWARD", 1430, 90, "Клавиатура: Назад")
                    print("[Ручной] -> НАЗАД")
                elif cmd in ["a", "влево", "налево"]:
                    spd = 1560 if self.mode != "BACKWARD" else 1440
                    self.set_action("LEFT", spd, 75, "Клавиатура: Налево")
                    print("[Ручной] -> НАЛЕВО")
                elif cmd in ["d", "вправо", "направо"]:
                    spd = 1560 if self.mode != "BACKWARD" else 1440
                    self.set_action("RIGHT", spd, 105, "Клавиатура: Направо")
                    print("[Ручной] -> НАПРАВО")
                elif cmd in [" ", "space", "стоп", "стой", "x"]:
                    self.set_action("STOP", 1500, 90, "Клавиатура: Стоп")
                    print("[Ручной] -> СТОП")
                elif cmd in ["+", "быстрее"]:
                    if self.mode == "FORWARD":
                        self.speed = min(1640, self.speed + 25)
                    elif self.mode == "BACKWARD":
                        self.speed = max(1360, self.speed - 25)
                    print(f"[Ручной] -> Скорость: {self.speed}")
                elif cmd in ["-", "тише", "медленнее"]:
                    if self.mode == "FORWARD":
                        self.speed = max(1520, self.speed - 25)
                    elif self.mode == "BACKWARD":
                        self.speed = min(1480, self.speed + 25)
                    print(f"[Ручной] -> Скорость: {self.speed}")
                elif cmd == "склад":
                    self.set_action("SKLAD", 1500, 90, "Клавиатура: Склад")
                    print("[Ручной] -> СКЛАД")
                elif cmd == "гараж":
                    self.set_action("GARAZH", 1500, 90, "Клавиатура: Гараж")
                    print("[Ручной] -> ГАРАЖ")
                else:
                    parsed = self.parse_phrase(cmd)
                    if parsed:
                        m, spd, ang, desc = parsed
                        self.set_action(m, spd, ang, f"Текст: {cmd}")
                        print(f"[Ручной] -> {desc} ({m})")
                    else:
                        print(f"[Инфо]   Неизвестная команда: '{cmd}'")
        except KeyboardInterrupt:
            print("\n[Выход]  Остановка по Ctrl+C...")
        finally:
            self.running = False
            self.set_action("STOP", 1500, 90)
            time.sleep(0.3)
            print("[Конец]  Сервер остановлен. Все соединения закрыты.")

if __name__ == "__main__":
    port_num = 9090
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port_num = int(sys.argv[1])
    station = VoiceStation(port=port_num)
    station.run()
