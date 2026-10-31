# -*- coding: utf-8 -*-
"""
Бортовой клиент для робота EyeCar (потоковый режим с автостопом по watchdog).
Работает в связке с сервером server.py.

Особенности архитектуры:
- Аппаратное управление через pigpio (ESC пин 17, рулевой сервопривод пин 18).
- Сторожевой таймер (Watchdog): таймаут сокета равен 1.0 сек. Если за секунду от сервера
  не пришло ни одного пакета, клиент считает связь потерянной и мгновенно глушит моторы.
- Поддержка автономных траекторных подпрограмм (рутин):
  * run_sklad_routine — проезд на склад с объездом препятствия.
  * run_garazh_routine — заезд и парковка в зоне гаража.
- Поддержка виртуальной эмуляции без Raspberry Pi для отладки.
"""

import os
import sys
import time
import socket
import signal

# Корректная кодировка для консоли
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

try:
    import pigpio
except ImportError:
    pigpio = None

def init_hardware():
    """
    Инициализирует подключение к демону pigpiod и калибрует пины моторов.
    Возвращает (pi, esc_pin, steer_pin) либо (None, None, None) при ошибке/симуляции.
    """
    if pigpio is None:
        return None, None, None
    try:
        os.system("sudo pigpiod > /dev/null 2>&1")
        time.sleep(0.5)
        pi = pigpio.pi()
        if not pi.connected:
            return None, None, None

        esc_pin = 17    # Силовой регулятор мотора
        steer_pin = 18  # Сервопривод поворота колес
        
        # Сброс сигналов
        pi.set_servo_pulsewidth(esc_pin, 0)
        pi.set_servo_pulsewidth(steer_pin, 0)
        time.sleep(0.5)
        
        # Выставление в нейтраль (1500 мкс)
        pi.set_servo_pulsewidth(esc_pin, 1500)
        pi.set_servo_pulsewidth(steer_pin, 1500)
        return pi, esc_pin, steer_pin
    except Exception:
        return None, None, None

def set_motors(pi, esc, steer, speed, angle):
    """
    Устанавливает скорость мотора и угол сервопривода с аппаратными ограничениями.
    """
    if pi is not None:
        # Ограничение диапазона ШИМ для регулятора
        s = max(1350, min(1650, int(speed)))
        # Ограничение угла поворота в градусах
        a = max(70, min(110, int(angle)))
        pi.set_servo_pulsewidth(esc, s)
        # Пересчет градусов в ширину импульса
        pulse = int(11.1 * a + 500)
        pi.set_servo_pulsewidth(steer, pulse)

def stop_motors(pi, esc, steer):
    """
    Переводит мотор и руль в безопасную нейтраль (1500 мкс / 90 градусов).
    """
    if pi is not None:
        set_motors(pi, esc, steer, 1500, 90)

def run_sklad_routine(pi, esc, steer):
    """
    Автономная подпрограмма: доставка груза в зону 'Склад'.
    Выполняет выверенную последовательность маневров с фиксированными паузами.
    """
    print("\n[Манёвр]  Выполняю автономный маршрут доставки на СКЛАД...")
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(0.5)
    set_motors(pi, esc, steer, 1580, 70)
    time.sleep(1.1)
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(0.5)
    set_motors(pi, esc, steer, 1580, 110)
    time.sleep(1.1)
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(1.0)
    stop_motors(pi, esc, steer)
    print("[Манёвр]  Груз доставлен на склад!\n")

def run_garazh_routine(pi, esc, steer):
    """
    Автономная подпрограмма: заезд и аккуратная парковка в 'Гараж'.
    """
    print("\n[Манёвр]  Выполняю автономный заезд в ГАРАЖ...")
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(0.5)
    set_motors(pi, esc, steer, 1580, 110)
    time.sleep(1.3)
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(0.5)
    set_motors(pi, esc, steer, 1580, 70)
    time.sleep(0.7)
    set_motors(pi, esc, steer, 1500, 90)
    time.sleep(1.0)
    stop_motors(pi, esc, steer)
    print("[Манёвр]  Успешная парковка в гараже!\n")

def main():
    print("=" * 60)
    print("            БОРТОВОЙ КЛИЕНТ РОБОТА (EYECAR)")
    print("=" * 60)

    pi, esc, steer = init_hardware()
    mode_name = "Аппаратный EyeCar (pigpio GPIO)" if pi is not None else "Виртуальный режим (симуляция)"
    print(f"  • Статус платформы: {mode_name}")
    print(f"  • Защита: автоостановка при потере связи > 1.0 сек")
    print("-" * 60)

    host = "127.0.0.1"
    port = 9090

    # Аргументы командной строки или интерактивный запрос
    if len(sys.argv) > 1:
        host = sys.argv[1]
    else:
        user_host = input("Введите IP адрес сервера [Enter = 127.0.0.1]: ").strip()
        if user_host:
            host = user_host

    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        port = int(sys.argv[2])

    print(f"\n[Подключение]  Ищу сервер {host}:{port}...")

    # Цикл переподключения до установления связи
    sock = None
    while sock is None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((host, port))
            sock = s
        except Exception:
            print(f"[Ожидание]     Сервер {host}:{port} пока не отвечает. Повтор через секунду...")
            time.sleep(1.0)

    print(f"[Успех]        Связь с сервером установлена! Робот готов к движению.\n")
    # Включаем жесткий таймаут 1 секунда для реализации watchdog
    sock.settimeout(1.0)

    last_mode = None
    last_speed = None
    last_angle = None
    warning_shown = False

    try:
        buffer = ""
        while True:
            try:
                chunk = sock.recv(256).decode('utf-8', errors='ignore')
                if not chunk:
                    print("\n[Сеть]         Сервер закрыл соединение.")
                    break
                buffer += chunk
                warning_shown = False
            except socket.timeout:
                # Сторожевой таймер: пакетов нет более 1 секунды — глушим моторы!
                if not warning_shown:
                    print("\n[Внимание!]    Нет связи с сервером > 1.0 сек! Аварийная остановка моторов.")
                    stop_motors(pi, esc, steer)
                    warning_shown = True
                continue

            lines = buffer.split('\n')
            buffer = lines[-1]
            valid_lines = [l.strip() for l in lines[:-1] if l.strip()]
            if not valid_lines:
                continue

            # Берем самую свежую строчку из буфера
            latest = valid_lines[-1]
            parts = latest.split()
            if len(parts) < 3:
                continue

            action = parts[0].upper()
            try:
                speed_val = int(parts[1])
                angle_val = int(parts[2])
            except ValueError:
                continue

            # Обработка макрокоманд маршрутов
            if action == "SKLAD":
                run_sklad_routine(pi, esc, steer)
                last_mode = "SKLAD"
                continue
            elif action == "GARAZH":
                run_garazh_routine(pi, esc, steer)
                last_mode = "GARAZH"
                continue

            # Красивый вывод изменения состояния в консоль
            if (action != last_mode) or (speed_val != last_speed) or (angle_val != last_angle):
                last_mode = action
                last_speed = speed_val
                last_angle = angle_val

                direction_symbol = {
                    "FORWARD": "[ ▲ ВПЕРЁД ]",
                    "BACKWARD": "[ ▼ НАЗАД  ]",
                    "LEFT": "[ ◄ НАЛЕВО ]",
                    "RIGHT": "[ ► НАПРАВО]",
                    "STOP": "[ ■ СТОП   ]"
                }.get(action, f"[ {action} ]")

                print(f"[Команда]  {direction_symbol:<14} |  Газ: {speed_val:<5} |  Руль: {angle_val:>3}°")

            # Применение значений к сервоприводам
            set_motors(pi, esc, steer, speed_val, angle_val)

    except KeyboardInterrupt:
        print("\n\n[Остановка]    Завершение работы оператором (Ctrl+C)...")
    finally:
        # Всегда останавливаем моторы при выходе
        stop_motors(pi, esc, steer)
        if pi is not None:
            try:
                pi.set_servo_pulsewidth(esc, 0)
                pi.set_servo_pulsewidth(steer, 0)
                pi.stop()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        print("[Клиент]: Выключение завершено.")

if __name__ == "__main__":
    main()
