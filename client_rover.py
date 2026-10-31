# -*- coding: utf-8 -*-
"""
Бортовой клиент для шестиколесного мобильного робота-вездехода (Ровер / Манул).
Работает в среде ROS 2 (Humble / Foxy / Galactic) на бортовом компьютере платформы.

Особенности управления:
- Шестиколесный привод с разделением по бортам (скид-стиринг / бортовой поворот).
- Управление скоростью через публикацию пакетов типа Int16MultiArray в топик /speed.
  Формат данных топика: [right, right, right, left, left, left] (три правых и три левых колеса).
- Автоматическое считывание ROS_DOMAIN_ID из стартового скрипта робота ~/start_manul_services.sh.
- Защита от потери команд и автостоп при завершении работы.
"""

import os
import re
import sys
import time
import signal
import socket

# Подтягиваем системный идентификатор домена ROS 2 из конфигурационного файла Манула.
# Это необходимо, чтобы нода и сервисы робота видели друг друга в сети DDS.
try:
    settings = open(os.path.expanduser("~/start_manul_services.sh")).read()
    os.environ["ROS_DOMAIN_ID"] = re.search(r"^export ROS_DOMAIN_ID=(\d+)", settings, re.M).group(1)
    os.environ["FASTDDS_BUILTIN_TRANSPORTS"] = "UDPv4"
except Exception:
    pass

# Попытка импорта библиотек ROS 2
try:
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from std_msgs.msg import Int16MultiArray
    has_ros2 = True
except ImportError:
    has_ros2 = False

def setup_ros():
    """
    Инициализирует контекст rclpy и создает издатель скоростей колес.
    """
    if not has_ros2:
        return (None, None)
    try:
        # Инициализируем ROS 2 без перехвата системных сигналов, чтобы корректно работал Ctrl+C
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.default_int_handler)
            
        node = rclpy.create_node("my_rover")
        speed_pub = node.create_publisher(Int16MultiArray, "/speed", 10)
        
        # Ожидаем появления подписчика на стороне низкоуровневого драйвера шасси
        for _ in range(50):
            if speed_pub.get_subscription_count() > 0:
                break
            time.sleep(0.1)
        if speed_pub.get_subscription_count() == 0:
            print("Ровер не слышит программу: подождите минуту после включения или позовите куратора")
        return (node, speed_pub)
    except Exception as e:
        print("Ошибка инициализации ROS 2 ровера: {}".format(e))
        return (None, None)

def move(speed_pub, left, right, duration):
    """
    Управляет скоростями бортов ровера на протяжении заданного времени.
    
    left: скорость левого борта (-80..80)
    right: скорость правого борта (-80..80)
    duration: длительность движения в секундах
    """
    if speed_pub is None:
        return
    # Программное ограничение максимальной тяги
    left = max(-80, min(80, int(left)))
    right = max(-80, min(80, int(right)))
    
    # Пакет для всех шести колес: первые три — правый борт, вторые три — левый
    msg = Int16MultiArray(data=[right, right, right, left, left, left])
    
    # Непрерывно шлем пакеты с частотой 10 Гц на протяжении duration
    for _ in range(int(duration * 10)):
        speed_pub.publish(msg)
        time.sleep(0.1)
        
    # По истечении времени обязательно тормозим
    stop(speed_pub)

def stop(speed_pub):
    """
    Гарантированная остановка моторов: отправка пачки нулевых пакетов.
    """
    if speed_pub is None:
        return
    for _ in range(5):
        speed_pub.publish(Int16MultiArray(data=[0, 0, 0, 0, 0, 0]))
        time.sleep(0.1)

def main():
    print("=" * 50)
    print("        БОРТОВОЙ КЛИЕНТ ДЛЯ РОВЕРА")
    print("=" * 50)

    node, speed_pub = setup_ros()
    if speed_pub is not None:
        print("[Ровер]: ROS 2 подключен (/speed готов)")
    else:
        print("[Ровер]: Режим без ROS 2 (тестовая эмуляция)")

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9090

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("Подключаюсь к серверу {}:{}...".format(host, port))
    sock.connect((host, port))
    
    # Регистрация на сервере-диспетчере
    sock.sendall(b"HELLO rover\n")
    print("[Ровер]: Связь установлена! Готов выполнять команды.\n")

    try:
        buf = ""
        while True:
            data = sock.recv(128).decode('utf-8', errors='ignore')
            if not data:
                break
            buf += data
            while "\n" in buf:
                cmd_line, buf = buf.split("\n", 1)
                cmd_line = cmd_line.strip()
                if not cmd_line:
                    continue

                parts = cmd_line.split()
                act = parts[0]
                dur = float(parts[1]) if len(parts) > 1 else 6.0

                print("Команда: {}, время: {} сек".format(act, dur))

                # Дифференциальное управление платформой
                if act == "FORWARD":
                    # Движение вперед с равной скоростью бортов
                    move(speed_pub, 50, 50, dur)

                elif act == "BACKWARD":
                    # Движение назад
                    move(speed_pub, -50, -50, dur)

                elif act == "LEFT":
                    # Плавный поворот налево (левый борт медленнее правого)
                    move(speed_pub, 35, 65, dur)

                elif act == "RIGHT":
                    # Плавный поворот направо (правый борт медленнее левого)
                    move(speed_pub, 65, 35, dur)

                elif act == "CIRCLE":
                    # Разворот по дуге (для ровера обычно 6 секунд)
                    circle_time = dur if dur > 3.0 else 6.0
                    move(speed_pub, 70, 35, circle_time)

                elif act == "STOP":
                    stop(speed_pub)

    except Exception as e:
        print("[Ровер]: Ошибка связи/выполнения: {}".format(e))
    finally:
        stop(speed_pub)
        if has_ros2:
            try:
                rclpy.shutdown()
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass
        print("[Ровер]: Программа завершена, моторы остановлены.")

if __name__ == "__main__":
    main()
