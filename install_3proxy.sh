#!/bin/bash

# Путь, куда будет установлен 3proxy
INSTALL_DIR="./3proxy_binaries"
# URL репозитория 3proxy
THREED_PROXY_REPO="https://github.com/z3apa3a/3proxy.git"
THREED_PROXY_DIR="3proxy" # Имя директории после клонирования

echo "Начинаем установку 3proxy в $INSTALL_DIR"

# 1. Проверка наличия make, gcc и git
echo "Проверка make..."
if ! command -v make &> /dev/null
then
    echo "make не найден. Пожалуйста, установите make: sudo apt update && sudo apt install make"
    exit 1
fi

echo "Проверка gcc..."
if ! command -v gcc &> /dev/null
then
    echo "gcc не найден. Пожалуйста, установите gcc: sudo apt update && sudo apt install build-essential"
    exit 1
fi

echo "Проверка git..."
if ! command -v git &> /dev/null
then
    echo "git не найден. Пожалуйста, установите git: sudo apt update && sudo apt install git"
    exit 1
fi

# 2. Создание директории для установки, если она не существует
mkdir -p "$INSTALL_DIR"
if [ $? -ne 0 ]; then
    echo "Ошибка: Не удалось создать директорию $INSTALL_DIR"
    exit 1
fi

# 3. Клонирование репозитория 3proxy
echo "Клонирование 3proxy репозитория из $THREED_PROXY_REPO..."
git clone "$THREED_PROXY_REPO" "$THREED_PROXY_DIR"
if [ $? -ne 0 ]; then
    echo "Ошибка: Не удалось клонировать репозиторий 3proxy. Проверьте URL или подключение к интернету."
    exit 1
fi

# 4. Компиляция 3proxy
echo "Компиляция 3proxy..."
cd "$THREED_PROXY_DIR"
# Создаем символическую ссылку на Makefile.Linux
ln -s Makefile.Linux Makefile
make
if [ $? -ne 0 ]; then
    echo "Ошибка: Не удалось скомпилировать 3proxy. Проверьте логи компиляции."
    cd ..
    exit 1
fi

# 5. Копирование скомпилированного бинарника
echo "Копирование 3proxy в $INSTALL_DIR..."
cp "bin/3proxy" "../$INSTALL_DIR/3proxy"
if [ $? -ne 0 ]; then
    echo "Ошибка: Не удалось скопировать скомпилированный 3proxy."
    cd ..
    exit 1
fi

# 6. Установка прав на исполнение
echo "Установка прав на исполнение для 3proxy..."
chmod +x "../$INSTALL_DIR/3proxy"
if [ $? -ne 0 ]; then
    echo "Ошибка: Не удалось установить права на исполнение."
    cd ..
    exit 1
fi

# 7. Очистка временных файлов
echo "Очистка временных файлов..."
cd ..
rm -rf "$THREED_PROXY_DIR"

echo "3proxy успешно установлен в $INSTALL_DIR/3proxy"