# -*- coding: utf-8 -*-
"""
Бортовой клиент для роботизированного автомобиля EyeCar (Икар).
Запускается на бортовом компьютере Raspberry Pi.

Аппаратное управление:
- Драйвер мотора (ESC): подключен к GPIO 17 (ШИМ 50 Гц).
  Нейтраль — 1500 мкс, плавный передний ход — ~1560 мкс, задний ход — ~1440 мкс.
- Рулевой сервопривод: подключен к GPIO 18 (ШИМ 50 Гц).
  Центральное положение — 90°, максимальный поворот влево — 110°, вправо — 70°.

Сетевой протокол:
- TCP-клиент подключается к диспетчеру (порт 9090).
- Приветствие: 'HELLO eyecar\\n'.
- Формат команд: '<ACTION> <DURATION>\\n'.
- Безопасность: при выходе или сбое моторы принудительно переводятся в нейтраль.
"""

import os
import sys
import signal
import time
import socket

# Библиотека pigpio нужна для точной генерации ШИМ на GPIO Raspberry Pi.
# Если скрипт запускается на тестовом компьютере без Raspberry Pi,
# pigpio перехватывается, и клиент работает в режиме безопасной симуляции.
try:
    import pigpio
except ImportError:
    pigpio = None

def setup_gpio():
    """
    Инициализирует демон pigpiod и настраивает пины ESC и рулевой машинки.
    """
    if pigpio is None:
        return (None, 17, 18)
    # Запускаем демон pigpiod, если он еще не поднят
    os.system("sudo pigpiod")
    time.sleep(1)
    
    ESC = 17    # Пин регулятора скорости (ходовой мотор)
    STEER = 18  # Пин сервопривода рулевой рейки
    
    pi = pigpio.pi()
    # Базовая частота для стандартных модельных сервоприводов и регуляторов — 50 Гц
    pi.set_PWM_frequency(ESC, 50)
    pi.set_PWM_frequency(STEER, 50)
    
    # Первичная калибровка: выставляем нейтраль (1500 мкс)
    pi.set_servo_pulsewidth(ESC, 1500)
    pi.set_servo_pulsewidth(STEER, 1500)
    return (pi, ESC, STEER)

def control(pi, ESC, speed, STEER, angle):
    """
    Подает управляющие импульсы на мотор и руль с программным ограничением диапазонов.
    
    speed: ширина импульса в мкс (1400..1600)
    angle: угол колес в градусах (70..110, 90 — прямо)
    """
    # Защита от опасных значений скорости (чтобы машинка не врезалась в борт)
    if speed > 1600:
        speed = 1600
    elif speed < 1400:
        speed = 1400

    # Защита от выхода сервопривода за механические упоры
    if angle < 70:
        angle = 70
    elif angle > 110:
        angle = 110

    if pi is not None:
        pi.set_servo_pulsewidth(ESC, speed)
        # Линейный пересчет угла (70..110) в микросекунды импульса сервомашинки
        steer_pulse = int(11.1 * angle + 500)
        pi.set_servo_pulsewidth(STEER, steer_pulse)

def main():
    pi, ESC, STEER = setup_gpio()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.default_int_handler)

    # Адрес и порт диспетчера можно передать аргументами командной строки
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9090

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("Подключаюсь к серверу {}:{}...".format(host, port))
    sock.connect((host, port))
    
    # Регистрация на сервере-диспетчере
    sock.sendall(b"HELLO eyecar\n")

    try:
        # Устанавливаем нейтраль и даем секунду на стабилизацию питания
        control(pi, ESC, 1500, STEER, 90)
        print("Start")
        time.sleep(1)

        buf = ""
        while True:
            data = sock.recv(128).decode('utf-8', errors='ignore')
            if not data:
                break
            buf += data
            # Построчный разбор командного потока
            while "\n" in buf:
                cmd_line, buf = buf.split("\n", 1)
                cmd_line = cmd_line.strip()
                if not cmd_line:
                    continue

                parts = cmd_line.split()
                act = parts[0]
                dur = float(parts[1]) if len(parts) > 1 else 3.0

                print("Команда: {}, время: {} сек".format(act, dur))

                # Выполнение маневров с автоматическим возвратом в нейтраль после истечения таймера
                if act == "FORWARD":
                    control(pi, ESC, 1560, STEER, 90)
                    time.sleep(dur)
                    control(pi, ESC, 1500, STEER, 90)

                elif act == "BACKWARD":
                    control(pi, ESC, 1440, STEER, 90)
                    time.sleep(dur)
                    control(pi, ESC, 1500, STEER, 90)

                elif act == "LEFT":
                    control(pi, ESC, 1560, STEER, 110)
                    time.sleep(dur)
                    control(pi, ESC, 1500, STEER, 90)

                elif act == "RIGHT":
                    control(pi, ESC, 1560, STEER, 70)
                    time.sleep(dur)
                    control(pi, ESC, 1500, STEER, 90)

                elif act == "CIRCLE":
                    # Для машинки круг занимает около 7 секунд
                    circle_time = dur if (dur > 3.0) else 7.0
                    control(pi, ESC, 1560, STEER, 110)
                    time.sleep(circle_time)
                    control(pi, ESC, 1500, STEER, 90)

                elif act == "STOP":
                    control(pi, ESC, 1500, STEER, 90)

    except Exception as e:
        print("[EyeCar]: Ошибка связи/выполнения: {}".format(e))
    finally:
        # Критически важная секция безопасности:
        # Гарантируем сброс мотора в нейтраль при любом исходе (Ctrl+C, разрыв связи, ошибка)
        control(pi, ESC, 1500, STEER, 90)
        time.sleep(0.5)
        if pi is not None:
            try:
                # Отключаем подачу импульсов на пины
                pi.set_servo_pulsewidth(ESC, 0)
                pi.set_servo_pulsewidth(STEER, 0)
                pi.stop()
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass
        print("[EyeCar]: Программа остановлена.")

if __name__ == "__main__":
    main()
