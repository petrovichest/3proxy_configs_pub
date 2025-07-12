#!/bin/bash

# Скрипт для автоматической установки Python 3, pip, venv и зависимостей

echo "Начинаем установку Python окружения..."

# 1. Обновление списка пакетов
echo "Обновление списка пакетов..."
sudo apt update || { echo "Ошибка: Не удалось обновить список пакетов. Проверьте подключение к интернету или права sudo."; exit 1; }

# 2. Проверка и установка python3-pip и python3-venv
echo "Проверка и установка python3-pip и python3-venv..."
sudo apt install -y python3-pip python3-venv || { echo "Ошибка: Не удалось установить python3-pip или python3-venv."; exit 1; }

# 3. Создание виртуального окружения
ENV_DIR="venv"
echo "Создание виртуального окружения в директории $ENV_DIR..."
python3 -m venv "$ENV_DIR" || { echo "Ошибка: Не удалось создать виртуальное окружение."; exit 1; }

# 4. Активация виртуального окружения и установка зависимостей
echo "Активация виртуального окружения и установка зависимостей из requirements.txt..."
# Проверим, существует ли requirements.txt
if [ ! -f "requirements.txt" ]; then
    echo "Ошибка: Файл requirements.txt не найден в текущей директории."
    exit 1
fi

source "$ENV_DIR/bin/activate" || { echo "Ошибка: Не удалось активировать виртуальное окружение."; exit 1; }
pip install -r requirements.txt || { echo "Ошибка: Не удалось установить зависимости из requirements.txt. Проверьте содержимое файла."; deactivate; exit 1; }

echo "Деактивация виртуального окружения..."
deactivate

echo "Установка Python окружения и зависимостей завершена успешно."
echo "Вы можете активировать виртуальное окружение, выполнив: source $ENV_DIR/bin/activate"