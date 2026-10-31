# -*- coding: utf-8 -*-
"""
Бортовой клиент для четвероногого робота-собаки PuppyPi.
Работает в среде ROS 1 (Robot Operating System Noetic/Melodic) на бортовом компьютере робопса.

Возможности интеграции с ROS:
- Управление локомоцией (ходьба вперед/назад, разворот на месте) через топик /puppy_control/velocity.
- Выбор походки (рысь trot, шаг walk) через /puppy_control/gait.
- Управление кинематической позой (наклон по тангажу, крен, клиренс) через /puppy_control/pose.
- Запуск пресетов трюков (сесть, лечь, дать лапу, потянуться) через сервис /puppy_control/runActionGroup.
- Быстрый сброс в устойчивую стойку через сервис /puppy_control/go_home.
- Белый список проверенных трюков (SAFE_TRICKS) для предотвращения опрокидывания робота.
"""

import os
import sys
import time
import math
import signal
import socket

# Попытка импорта библиотек ROS 1.
# Если скрипт запускается на компьютере разработчика без ROS,
# флаг has_ros выставляется в False, и код работает в режиме симуляции.
try:
    import rospy
    from puppy_control.msg import Velocity, Pose, Gait
    from puppy_control.srv import SetRunActionName
    from std_srvs.srv import Empty
    has_ros = True
except ImportError:
    has_ros = False

# Белый список безопасных анимаций из комплекта PuppyPi
SAFE_TRICKS = ["sit", "lie_down", "shake_hands", "nod", "wave", "stretch"]

vel_pub = None
pose_pub = None
gait_pub = None

def setup_ros():
    """
    Инициализирует ROS-ноду и паблишеры для управления кинематикой собаки.
    """
    global vel_pub, pose_pub, gait_pub
    if not has_ros:
        return
    try:
        rospy.init_node('my_puppy_program', anonymous=True, disable_signals=True)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.default_int_handler)
        
        # Издатели топиков управления позой, походкой и скоростью
        pose_pub = rospy.Publisher('/puppy_control/pose', Pose, queue_size=1)
        gait_pub = rospy.Publisher('/puppy_control/gait', Gait, queue_size=1)
        vel_pub = rospy.Publisher('/puppy_control/velocity', Velocity, queue_size=1)
        
        time.sleep(1)
        # Устанавливаем стабильную базовую походку «рысь» и нейтральную позу
        set_gait(gait_pub, "trot")
        set_pose(pose_pub)
    except Exception as e:
        print("Ошибка инициализации ROS собаки: {}".format(e))

def set_gait(gait_pub, gait="trot"):
    """
    Конфигурирует тайминги шага робота.
    'trot' (рысь) — более быстрая и динамичная походка.
    'walk' (шаг) — более устойчивая и плавная.
    """
    if not has_ros or gait_pub is None:
        return
    try:
        if gait == "walk":
            gait_pub.publish(Gait(overlap_time=0.1, swing_time=0.2, clearance_time=0.3, z_clearance=5.0))
        else:
            gait_pub.publish(Gait(overlap_time=0.2, swing_time=0.3, clearance_time=0.0, z_clearance=5.0))
            time.sleep(0.2)
    except Exception:
        pass

def move(vel_pub, speed, yaw_rate=0.0, duration=1.0):
    """
    Передает линейную скорость (speed) и угловую скорость вращения (yaw_rate).
    По истечении duration автоматически публикует команду нулевой скорости.
    """
    if not has_ros or vel_pub is None:
        return
    try:
        # Ограничиваем скорость в безопасных пределах (см/с)
        speed = max(-10.0, min(15.0, float(speed)))
        yaw_rate = max(-0.5, min(0.5, float(yaw_rate)))
        vel_pub.publish(Velocity(x=speed, y=0.0, yaw_rate=yaw_rate))
        time.sleep(duration)
        # Остановка движения
        vel_pub.publish(Velocity(x=0.0, y=0.0, yaw_rate=0.0))
    except Exception as e:
        print("Ошибка движения: {}".format(e))

def set_pose(pose_pub, height=-10.0, pitch=0.0, roll=0.0, duration=0.5):
    """
    Устанавливает статическую позу корпуса (высоту, наклон вперед/назад, крен).
    """
    if not has_ros or pose_pub is None:
        return
    try:
        height = max(-13.0, min(-5.0, float(height)))
        pitch_rad = math.radians(max(-15.0, min(15.0, float(pitch))))
        roll_rad = math.radians(max(-12.0, min(12.0, float(roll))))
        run_time = int(duration * 1000)
        msg = Pose(height=height, pitch=pitch_rad, roll=roll_rad, x_shift=-0.6, run_time=run_time)
        pose_pub.publish(msg)
        time.sleep(duration)
    except Exception as e:
        print("Ошибка установки позы: {}".format(e))

def action(name):
    """
    Вызывает файл анимации трюка из стандартной библиотеки действий PuppyPi.
    Обязательно проверяется наличие трюка в белом списке SAFE_TRICKS.
    """
    if not has_ros:
        return
    if name not in SAFE_TRICKS:
        print("Этот трюк запускать нельзя: {}".format(name))
        return
    try:
        run = rospy.ServiceProxy('/puppy_control/runActionGroup', SetRunActionName)
        run(name + '.d6ac', True)
    except Exception as e:
        print("Ошибка трюка {}: {}".format(name, e))

def stand():
    """
    Возвращает робота в исходное нейтральное положение стоя на четырех лапах.
    """
    if not has_ros:
        return
    try:
        rospy.ServiceProxy('/puppy_control/go_home', Empty)()
        time.sleep(1)
    except Exception as e:
        print("Ошибка стойки: {}".format(e))

def stop_dog():
    """
    Аварийная остановка: возвращает позу 'home' и троекратно отправляет нулевую скорость,
    чтобы гарантировать остановку даже при потере отдельных UDP/TCP-пакетов в ROS.
    """
    stand()
    if has_ros and vel_pub is not None:
        try:
            for _ in range(3):
                vel_pub.publish(Velocity(x=0.0, y=0.0, yaw_rate=0.0))
                time.sleep(0.05)
        except Exception:
            pass

def main():
    print("=" * 50)
    print("       БОРТОВОЙ КЛИЕНТ PUPPYPI (СОБАКА)")
    print("=" * 50)

    setup_ros()
    if has_ros:
        print("[PuppyPi]: Подключено к ROS собаки")
    else:
        print("[PuppyPi]: Запуск без ROS (тестовый режим)")

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9090

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("Подключаюсь к серверу {}:{}...".format(host, port))
    sock.connect((host, port))
    
    # Регистрация собаки на диспетчере
    sock.sendall(b"HELLO puppypi\n")
    print("[PuppyPi]: Сопряжение успешно! Жду команды.\n")

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
                dur = float(parts[1]) if len(parts) > 1 else 3.0

                print("Команда: {}, время: {} сек".format(act, dur))

                # Обработка команд движения и трюков
                if act == "FORWARD":
                    stand()
                    move(vel_pub, speed=7.0, duration=dur)
                    time.sleep(0.1)
                elif act == "BACKWARD":
                    stand()
                    move(vel_pub, speed=-7.0, duration=dur)
                    time.sleep(0.1)
                elif act == "LEFT":
                    stand()
                    move(vel_pub, speed=0.0, yaw_rate=0.3, duration=dur)
                    time.sleep(0.1)
                elif act == "RIGHT":
                    stand()
                    move(vel_pub, speed=0.0, yaw_rate=-0.3, duration=dur)
                    time.sleep(0.1)
                elif act == "SIT":
                    action("sit")
                    time.sleep(0.1)
                elif act == "STAND":
                    stand()
                    time.sleep(0.1)
                elif act == "LIE":
                    # Поза «лежать»: опускаем клиренс корпуса
                    set_pose(pose_pub, height=-5.0, pitch=0.0, roll=0.0, duration=0.8)
                    time.sleep(0.1)
                elif act == "PAW":
                    # Команда «дай лапу»: сначала посадка, затем анимация лапы
                    action("sit")
                    time.sleep(0.1)
                    action("shake_hands")
                    time.sleep(0.1)
                elif act in ["STRETCH", "BOW"]:
                    # Потягивание / поклон: наклон корпуса вперед
                    set_pose(pose_pub, pitch=-15.0, roll=0.0, duration=0.8)
                    time.sleep(0.1)
                elif act == "STOP":
                    stop_dog()

    except Exception as e:
        print("[PuppyPi]: Ошибка связи/выполнения: {}".format(e))
    finally:
        stop_dog()
        try:
            sock.close()
        except Exception:
            pass
        print("[PuppyPi]: Программа остановлена.")

if __name__ == "__main__":
    main()
