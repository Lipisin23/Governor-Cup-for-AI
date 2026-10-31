# -*- coding: utf-8 -*-
"""
Главная точка входа для запуска диспетчерской системы.

Скрипт выполняет первичную инициализацию окружения:
1. Принудительно выставляет UTF-8 для стандартных потоков вывода (актуально для Windows-консолей).
2. Переходит в рабочую директорию проекта, чтобы относительные пути к моделям и логам работали корректно.
3. Запускает главный управляющий модуль agent_system.
"""

import os
import sys

# Настройка UTF-8 для корректного вывода русских символов в терминале
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Гарантируем, что рабочей директорией является папка со скриптом
base_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(base_dir)

from agent_system import main as start_agent

if __name__ == "__main__":
    start_agent()
