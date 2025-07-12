#!/bin/bash

echo "Начинаем установку PM2 и Node.js..."

# Функция для проверки наличия команды
command_exists () {
  command -v "$1" >/dev/null 2>&1
}

# 1. Проверка и установка Node.js и npm
if ! command_exists node; then
    echo "Node.js не найден. Установка Node.js и npm..."
    sudo apt update -y
    sudo apt install -y curl
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt install -y nodejs
    if ! command_exists node; then
        echo "Ошибка: Не удалось установить Node.js. Проверьте логи установки."
        exit 1
    fi
    echo "Node.js и npm успешно установлены."
else
    echo "Node.js уже установлен."
fi

# 2. Установка PM2 глобально
echo "Установка PM2 глобально..."
sudo npm install -g pm2
if ! command_exists pm2; then
    echo "Ошибка: Не удалось установить PM2. Проверьте логи установки."
    exit 1
fi

echo "PM2 успешно установлен."