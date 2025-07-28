import os
import secrets
import string
import json
import argparse
from datetime import datetime
import re
import subprocess # Добавляем импорт для выполнения внешних команд
import sys # Добавляем импорт для sys
import ipaddress # Добавляем импорт для работы с IP-адресами
# Примеры использования:
# python3 1_generate_proxy_configs.py 20 MyProject
# (сгенерирует 20 прокси для проекта "MyProject")
#
# python3 1_generate_proxy_configs.py 5 AnotherProject
# (сгенерирует 5 прокси для проекта "AnotherProject")


BASE_OUTPUT_DIR = "generated_proxy_configs"
STATE_FILE = os.path.join(BASE_OUTPUT_DIR, "proxy_states.json")

# Общие настройки 3proxy
THREE_PROXY_HEADERS_TEMPLATE = """
maxconn 10000
nscache 65536
timeouts 1 5 30 60 180 1800 15 60
setgid 65535
setuid 65535
flush
auth strong
users {username}:CL:{password}
allow {username}
"""

# Внутренние параметры, которые не нужно указывать при запуске
DEFAULT_START_PORT = 10000
DEFAULT_END_PORT = 65000

def generate_random_string(length=10):
    """Генерирует случайную строку заданной длины."""
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for i in range(length))

def get_state():
    """Загружает текущее состояние из файла JSON."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {} # Return empty if JSON is malformed
    return {}

def save_state(state):
    """Сохраняет текущее состояние в файл JSON."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def validate_ipv4(ipv4_address):
    """Проверяет, является ли строка корректным IPv4-адресом."""
    pattern = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
    if not pattern.match(ipv4_address):
        raise ValueError(f"Некорректный формат IPv4: {ipv4_address}")
    # Проверка на диапазоны октетов (0-255)
    for part in ipv4_address.split('.'):
        if not 0 <= int(part) <= 255:
            raise ValueError(f"Некорректный октет IPv4: {part}")
    return True

# Функция get_external_ipv4 удалена, т.к. IPv4 будет передаваться аргументом

def check_and_add_ipv6_default_route(ipv6_subnet, interface):
    """
    Проверяет существование IPv6 маршрута по умолчанию и добавляет его, если он отсутствует.
    Первым доступным IP в подсети считается шлюз.
    """
    try:
        # Проверяем, существует ли маршрут по умолчанию
        result = subprocess.run(['ip', '-6', 'route', 'show', 'default'], capture_output=True, text=True, check=False)
        if "default via" in result.stdout:
            print("IPv6 маршрут по умолчанию уже существует. Пропускаем добавление.")
            return

        # Если маршрут не существует, определяем шлюз как первый IP в подсети
        # Создаем объект IPv6Network
        ipv6_network = ipaddress.IPv6Network(ipv6_subnet, strict=True)
        # Получаем первый доступный IP-адрес в подсети для использования в качестве шлюза
        # Для /64 подсети, первый доступный адрес будет network_address + 1
        # Для других префиксов, просто используем network_address + 1, если это не будет адресом сети
        if ipv6_network.prefixlen <= 127: # убедимся, что это не /128
            gateway_ip_int = int(ipv6_network.network_address) + 1
            gateway_ip = str(ipaddress.IPv6Address(gateway_ip_int))
        else:
            print(f"Предупреждение: Подсеть {ipv6_subnet} слишком мала для определения шлюза (+1). Используем адрес сети как шлюз.", file=sys.stderr)
            gateway_ip = str(ipv6_network.network_address)

        print(f"Добавляем IPv6 маршрут по умолчанию через {gateway_ip} на интерфейс {interface}...")
        add_route_command = ['sudo', 'ip', '-6', 'route', 'add', 'default', 'via', gateway_ip, 'dev', interface]
        subprocess.run(add_route_command, check=True)
        print("IPv6 маршрут по умолчанию успешно добавлен.")

    except subprocess.CalledProcessError as e:
        print(f"Ошибка при добавлении IPv6 маршрута по умолчанию: {e}", file=sys.stderr)
        print(f"Команда: {' '.join(e.cmd)}", file=sys.stderr)
    except ipaddress.AddressValueError as e:
        print(f"Ошибка: Некорректный формат IPv6 подсети '{ipv6_subnet}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"Неожиданная ошибка при работе с IPv6 маршрутом: {e}", file=sys.stderr)

def bind_ipv6_address(ipv6_subnet, interface):
    """
    Привязывает первый доступный IPv6-адрес из подсети к указанному интерфейсу.
    """
    try:
        ipv6_network = ipaddress.IPv6Network(ipv6_subnet, strict=True)
        # Первый доступный IP-адрес для привязки
        address_to_bind = str(ipv6_network.network_address + 2)
        
        # Проверяем, привязан ли уже этот адрес к интерфейсу
        check_command = ['ip', '-6', 'addr', 'show', 'dev', interface]
        result = subprocess.run(check_command, capture_output=True, text=True, check=True)
        if address_to_bind in result.stdout:
            print(f"IPv6 адрес {address_to_bind} уже привязан к {interface}. Пропускаем привязку.")
            return

        print(f"Привязка IPv6-адреса {address_to_bind} к интерфейсу {interface}...")
        command = ['sudo', 'ip', '-6', 'addr', 'add', f"{address_to_bind}/{ipv6_network.prefixlen}", 'dev', interface]
        subprocess.run(command, check=True)
        print(f"IPv6-адрес {address_to_bind} успешно привязан к {interface}.")
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при привязке IPv6-адреса: {e}", file=sys.stderr)
        print(f"Команда: {' '.join(e.cmd)}", file=sys.stderr)
    except ipaddress.AddressValueError as e:
        print(f"Ошибка: Некорректный формат IPv6 подсети '{ipv6_subnet}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"Неожиданная ошибка при привязке IPv6-адреса: {e}", file=sys.stderr)

def extract_proxies_from_content(file_content, output_file_path, username, password):
    """
    Извлекает прокси-серверы из содержимого конфигурации 3proxy, форматирует их
    и сохраняет в новый файл. Это интегрированная версия extract_proxies.
    """
    extracted_count = 0
    proxies_list = []

    for line in file_content.splitlines():
        if line.strip().startswith("proxy"):
            ipv4_match = re.search(r'-i(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', line)
            port_match = re.search(r'-p(\d+)', line)

            if ipv4_match and port_match:
                ipv4_address = ipv4_match.group(1)
                port = port_match.group(1)
                proxy_string = f"{ipv4_address}:{port}@{username}:{password}"
                proxies_list.append(proxy_string)
                extracted_count += 1
    
    with open(output_file_path, 'w') as f:
        for proxy in proxies_list:
            f.write(proxy + '\n')

    print(f"Извлечено и сохранено {extracted_count} прокси в файл '{output_file_path}'.")

def generate_proxy_configs(
    num_proxies,
    project_name,
    ipv6_subnet,
    interface,
    external_ipv4, # Добавляем внешний IPv4 как аргумент
):
    """
    Генерирует конфигурации прокси для указанного проекта.
    Использует внутренние параметры для портов и IPv6.
    """
    # Перед проверкой и добавлением маршрута, убедимся, что к интерфейсу привязан хотя бы один IPv6 адрес
    bind_ipv6_address(ipv6_subnet, interface)
    # Проверяем и добавляем IPv6 маршрут по умолчанию, если необходимо
    check_and_add_ipv6_default_route(ipv6_subnet, interface)

    project_output_dir = os.path.join(BASE_OUTPUT_DIR, project_name)
    os.makedirs(project_output_dir, exist_ok=True)

    session_output_dir = project_output_dir
    os.makedirs(session_output_dir, exist_ok=True) # This still needs to ensure the base project directory exists

    state = get_state()
    # external_ipv4 теперь передается как аргумент, автоматическое определение удалено
    try:
        ipv6_network = ipaddress.IPv6Network(ipv6_subnet, strict=True)
    except ipaddress.AddressValueError as e:
        print(f"Ошибка: Некорректный формат IPv6 подсети '{ipv6_subnet}': {e}", file=sys.stderr)
        sys.exit(1)
    
    current_ipv6_base_network_str = str(ipv6_network) # Для сохранения в состоянии

    # ******************* Создание setup_network_ipv6.sh *******************
    primary_ipv6_address = str(ipv6_network.network_address + 2) # Основной IP
    gateway_ipv6_address = str(ipv6_network.network_address + 1) # Шлюз

    setup_network_script_content = f"""#!/bin/bash
echo "Настройка основного IPv6-адреса и маршрута по умолчанию..."

# Проверяем, привязан ли этот адрес, чтобы избежать ошибок
if ! ip -6 addr show dev {interface} | grep -q "{primary_ipv6_address}"; then
    echo "Привязка IPv6-адреса {primary_ipv6_address}/{ipv6_network.prefixlen} к интерфейсу {interface}..."
    sudo ip -6 addr add {primary_ipv6_address}/{ipv6_network.prefixlen} dev {interface}
fi

# Проверяем, существует ли маршрут по умолчанию, чтобы избежать ошибок
if ! ip -6 route show default | grep -q "{gateway_ipv6_address}"; then
    echo "Добавление IPv6 маршрута по умолчанию через {gateway_ipv6_address} на интерфейс {interface}..."
    sudo ip -6 route add default via {gateway_ipv6_address} dev {interface} onlink
fi

# Запуск скрипта привязки всех прокси-адресов
# Предполагается, что 2_bind_ipv6_addresses.py находится на два уровня выше
BASE_DIR=$(cd $(dirname "${{BASH_SOURCE[0]}}")/../.. && pwd)
PYTHON_SCRIPT="$BASE_DIR/2_bind_ipv6_addresses.py"
PROJECT_NAME="{project_name}"

echo "Запуск привязки всех прокси IPv6-адресов для проекта {project_name}..."
# Активируем виртуальное окружение, если оно существует
if [ -f "$BASE_DIR/venv/bin/activate" ]; then
    source "$BASE_DIR/venv/bin/activate"
fi
sudo "$BASE_DIR/venv/bin/python" ${{PYTHON_SCRIPT}} ${{PROJECT_NAME}} --action add_all --interface {interface}

echo "Настройка сети IPv6 завершена."
"""
    setup_network_script_filename = os.path.join(session_output_dir, "setup_network_ipv6.sh")
    with open(setup_network_script_filename, "w") as f:
        f.write(setup_network_script_content)
    os.chmod(setup_network_script_filename, 0o755)
    print(f"Скрипт настройки сети IPv6: {setup_network_script_filename}")
    # *****************************************************************

    # Инициализация состояния для external_ipv4, если не существует
    if external_ipv4 not in state:
        state[external_ipv4] = {
            "latest_port": DEFAULT_START_PORT - 1,
            "ipv6_subnets": {}
        }

    # Инициализация состояния для subnet_ipv6, если не существует для текущего external_ipv4
    if current_ipv6_base_network_str not in state[external_ipv4]["ipv6_subnets"]:
        state[external_ipv4]["ipv6_subnets"][current_ipv6_base_network_str] = {
            "latest_suffix_increment": 0x0000 # Инкремент для последних 64 бит
        }

    current_port = state[external_ipv4]["latest_port"] + 1
    current_ipv6_suffix_increment = state[external_ipv4]["ipv6_subnets"][current_ipv6_base_network_str]["latest_suffix_increment"]

    if current_port < DEFAULT_START_PORT:
        current_port = DEFAULT_START_PORT

    generated_count = 0
    proxy_lines = []
    credentials_list = []

    # Use project name as both username and password
    proxy_username = project_name
    proxy_password = project_name

    for i in range(num_proxies):
        if current_port > DEFAULT_END_PORT:
            print(f"Предупреждение: Достигнут конец диапазона портов ({DEFAULT_START_PORT}-{DEFAULT_END_PORT}). Сгенерировано {generated_count} прокси из {num_proxies}.")
            break

        # Определяем IPv6-адрес в зависимости от prefixlen
        bind_ipv6_prefixlen = 64 # Для привязки всегда используем /64
        if ipv6_network.prefixlen == 48:
            # Для /48 подсети, инкрементируем префикс /64.
            # current_ipv6_suffix_increment будет определять четвертый хекстет.
            # Например, если network_address = 2a12:5940:dfaa::/48
            # и current_ipv6_suffix_increment = 0x0001,
            # мы хотим получить 2a12:5940:dfaa:0001::, а затем добавить ::2
            
            # Получаем первые 48 бит (3 хекстета)
            base_address_parts = ipv6_network.exploded.split(':')[:3]
            
            # Формируем новую /64 подсеть
            # Избегаем создания прямой строки адреса для network_address_with_new_hextet,
            # вместо этого работаем с целыми числами для корректных вычислений.
            
            # Преобразуем первые 48 бит в целое число, сдвигаем на 16 бит влево (чтобы освободить место для 4-го хекстета)
            # Затем добавляем инкремент и сдвигаем на 64 бита влево (чтобы сформировать префикс /64)
            # После этого добавляем 2 для final_address_int в конце
            
            # network_address_int = int(ipaddress.IPv6Address(':'.join(base_address_parts) + '::'))
            # new_64_bit_prefix_int = network_address_int | (current_ipv6_suffix_increment << (128 - 64))
            
            # Более простой и правильный способ - формируем подсеть /64 и берем из неё адрес
            # Используем ipaddress.IPv6Network для правильного формирования /64 из /48
            new_subnet_str = f"{':'.join(base_address_parts)}:{format(current_ipv6_suffix_increment, 'x')}::{bind_ipv6_prefixlen}"
            try:
                new_64_subnet = ipaddress.IPv6Network(new_subnet_str, strict=False)
                # Берем, например, 2-й адрес в этой /64 подсети (::2) для прокси
                final_address_int = int(new_64_subnet.network_address) + 2
                ipv6_address = str(ipaddress.IPv6Address(final_address_int))
            except ipaddress.AddressValueError as e:
                print(f"Ошибка при формировании /64 подсети из /48 '{new_subnet_str}': {e}", file=sys.stderr)
                sys.exit(1)

        elif ipv6_network.prefixlen == 64:
            # Для /64 подсети, инкрементируем последние 64 бита (последний суффикс)
            # Прибавляем current_ipv6_suffix_increment к числовому представлению адреса сети
            # и получаем новый адрес.
            # Например, для 2a03:a03:c4:d::/64 и increment=1, хотим 2a03:a03:c4:d::1
            new_address_int = int(ipv6_network.network_address) + (current_ipv6_suffix_increment << 48) + 2 # Тут было +2, оставил как было
            ipv6_address = str(ipaddress.IPv6Address(new_address_int))
        else:
            print(f"Ошибка: Неподдерживаемая длина префикса IPv6: {ipv6_network.prefixlen}. Поддерживаются только /48 и /64.", file=sys.stderr)
            sys.exit(1)
            
        # Только строка прокси для основного файла конфига
        proxy_line = (
            f"proxy -64 -n -a -p{current_port} "
            f"-i{external_ipv4} -e{ipv6_address}"
        )
        proxy_lines.append(proxy_line)

        # Данные для файла с учетными данными
        # Сохраняем полный адрес с длиной префикса для корректной привязки
        credentials_list.append(
            f"user:{proxy_username} pass:{proxy_password} proxy_ip:{external_ipv4} proxy_port:{current_port} ipv6:{ipv6_address}/{bind_ipv6_prefixlen}"
        )

        current_port += 1
        current_ipv6_suffix_increment += 1
        generated_count += 1

    formatted_headers = THREE_PROXY_HEADERS_TEMPLATE.format(username=proxy_username, password=proxy_password)

    full_config_filename = os.path.join(session_output_dir, "full_proxy_config")
    with open(full_config_filename, "w") as f:
        f.write(formatted_headers) # Добавляем заголовки
        f.write("\n".join(proxy_lines) + "\n") # Добавляем пустую строку в конце для чистоты

    credentials_output_filename = os.path.join(session_output_dir, "proxy_configs")
    with open(credentials_output_filename, "w") as f:
        f.write("\n".join(credentials_list) + "\n")

    # Получаем содержимое full_proxy_config для извлечения прокси
    with open(full_config_filename, "r") as f:
        full_proxy_content = f.read()

    # ******************* Создание start.sh для PM2 *******************
    start_script_content = f"""#!/bin/bash
pm2 start {os.path.join("..", "..", "3proxy_binaries", "3proxy")} --name {project_name} -- {os.path.basename(full_config_filename)}
"""
    start_script_filename = os.path.join(session_output_dir, "start.sh")
    with open(start_script_filename, "w") as f:
        f.write(start_script_content)
    os.chmod(start_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт запуска PM2: {start_script_filename}")
    # *****************************************************************

    # ******************* Создание start_systemctl.sh *******************
    # Абсолютные пути для systemctl сервиса
    three_proxy_binary_abs_path = os.path.abspath(os.path.join(BASE_OUTPUT_DIR, os.pardir, "3proxy_binaries", "3proxy"))
    full_config_abs_path = os.path.abspath(full_config_filename)

    systemctl_service_content = f"""[Unit]
Description=3proxy Service for {project_name}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={os.path.dirname(full_config_abs_path)}
ExecStartPre=/bin/bash {os.path.abspath(setup_network_script_filename)}
ExecStart={three_proxy_binary_abs_path} {os.path.basename(full_config_abs_path)}
LimitNOFILE=65535
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

    start_systemctl_script_content = f"""#!/bin/bash

SERVICE_FILE_PATH="/etc/systemd/system/3proxy-{project_name}.service"

echo "Creating systemd service file: ${{SERVICE_FILE_PATH}}"
echo "{systemctl_service_content}" | sudo tee ${{SERVICE_FILE_PATH}} > /dev/null

echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling and starting service 3proxy-{project_name}..."
sudo systemctl enable --now 3proxy-{project_name}.service

echo "Status of service 3proxy-{project_name}:"
echo "Проверка статуса сервиса 3proxy-{project_name}:"
sudo systemctl is-active --quiet 3proxy-{project_name}.service && echo "Сервис активен." || echo "Сервис не активен."
sudo systemctl is-enabled --quiet 3proxy-{project_name}.service && echo "Сервис включен в автозагрузку." || echo "Сервис не включен в автозагрузку."
"""

    # Записываем start_systemctl.sh скрипт
    start_systemctl_script_filename = os.path.join(session_output_dir, "start_systemctl.sh")
    with open(start_systemctl_script_filename, "w") as f:
        f.write(start_systemctl_script_content)
    os.chmod(start_systemctl_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт запуска systemctl: {start_systemctl_script_filename}")
    # *****************************************************************

    # ******************* Создание stop_systemctl.sh *******************
    stop_systemctl_script_content = f"""#!/bin/bash

PROJECT_NAME="{project_name}"
SERVICE_FILE_PATH="/etc/systemd/system/3proxy-${{PROJECT_NAME}}.service"

echo "Остановка сервиса 3proxy-${{PROJECT_NAME}}..."
# Проверяем, существует ли сервис, прежде чем пытаться его остановить/отключить
if systemctl is-active --quiet 3proxy-${{PROJECT_NAME}}.service; then
    sudo systemctl stop 3proxy-${{PROJECT_NAME}}.service
fi

echo "Отключение сервиса 3proxy-${{PROJECT_NAME}}..."
if systemctl is-enabled --quiet 3proxy-${{PROJECT_NAME}}.service; then
    sudo systemctl disable 3proxy-${{PROJECT_NAME}}.service
fi

echo "Удаление файла сервиса: ${{SERVICE_FILE_PATH}}"
sudo rm -f ${{SERVICE_FILE_PATH}}

echo "Перезагрузка daemon systemd..."
sudo systemctl daemon-reload

echo "Сервис 3proxy-${{PROJECT_NAME}} удален."
"""
    # Записываем stop_systemctl.sh скрипт
    stop_systemctl_script_filename = os.path.join(session_output_dir, "stop_systemctl.sh")
    with open(stop_systemctl_script_filename, "w") as f:
        f.write(stop_systemctl_script_content)
    os.chmod(stop_systemctl_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт остановки и удаления systemctl: {stop_systemctl_script_filename}")
    # *****************************************************************


    # ******************* Создание proxy_checker.sh *******************
    proxy_checker_script_content = f"""#!/bin/bash
    BASE_DIR=$(cd $(dirname "${{BASH_SOURCE[0]}}")/../.. && pwd) # Определяем базовую директорию проекта
    source "$BASE_DIR/venv/bin/activate" # Активируем виртуальное окружение
    
    # Скрипт для запуска проверки прокси с использованием proxy_checker.py
    # Предполагается, что 4_proxy_checker.py находится на два уровня выше текущей директории
    
    PYTHON_SCRIPT="../../4_proxy_checker.py"

    # Проверяем, активно ли виртуальное окружение
    if [ -n "$VIRTUAL_ENV" ]; then
        PYTHON_EXEC="$VIRTUAL_ENV/bin/python"
    else
        PYTHON_EXEC="python3"
    fi
     
    ${{PYTHON_EXEC}} ${{PYTHON_SCRIPT}} --project-name {project_name} "$@"
    """
    proxy_checker_script_filename = os.path.join(session_output_dir, "proxy_checker.sh")
    with open(proxy_checker_script_filename, "w") as f:
        f.write(proxy_checker_script_content)
    os.chmod(proxy_checker_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт проверки прокси: {proxy_checker_script_filename}")
    # *****************************************************************

    # ******************* Создание bind.sh *******************
    bind_script_content = f"""#!/bin/bash
BASE_DIR=$(cd $(dirname "${{BASH_SOURCE[0]}}")/../.. && pwd) # Определяем базовую директорию проекта
source "$BASE_DIR/venv/bin/activate" # Активируем виртуальное окружение
 
    # Скрипт для привязки/отвязки IPv6-адресов с использованием 2_bind_ipv6_addresses.py
    # Предполагается, что 2_bind_ipv6_addresses.py находится на два уровня выше текущей директории
    # Пример использования: ./bind.sh --interface eth0 --action add
 
    PROJECT_NAME="{project_name}"
    PYTHON_SCRIPT="../../2_bind_ipv6_addresses.py"
 
    # Проверяем, активно ли виртуальное окружение
    if [ -n "$VIRTUAL_ENV" ]; then
        PYTHON_EXEC="$VIRTUAL_ENV/bin/python"
    else
        PYTHON_EXEC="python3"
    fi
 
    sudo ${{PYTHON_EXEC}} ${{PYTHON_SCRIPT}} ${{PROJECT_NAME}} --interface {interface} "$@"
    """
    bind_script_filename = os.path.join(session_output_dir, "bind.sh")
    with open(bind_script_filename, "w") as f:
        f.write(bind_script_content)
    os.chmod(bind_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт привязки IPv6: {bind_script_filename}")
    # *****************************************************************
    
    # ******************* Создание unbind.sh *******************
    # Этот скрипт будет отвязывать IPv6-адреса.
    unbind_script_content = f"""#!/bin/bash
BASE_DIR=$(cd $(dirname "${{BASH_SOURCE[0]}}")/../.. && pwd) # Определяем базовую директорию проекта
source "$BASE_DIR/venv/bin/activate" # Активируем виртуальное окружение
 
    # Скрипт для привязки/отвязки IPv6-адресов с использованием 2_bind_ipv6_addresses.py
    # Предполагается, что 2_bind_ipv6_addresses.py находится на два уровня выше текущей директории
    # Пример использования: ./unbind.sh --interface eth0
 
    PROJECT_NAME="{project_name}"
    PYTHON_SCRIPT="../../2_bind_ipv6_addresses.py"
 
    # Проверяем, активно ли виртуальное окружение
    if [ -n "$VIRTUAL_ENV" ]; then
        PYTHON_EXEC="$VIRTUAL_ENV/bin/python"
    else
        PYTHON_EXEC="python3"
    fi
 
    sudo ${{PYTHON_EXEC}} ${{PYTHON_SCRIPT}} ${{PROJECT_NAME}} --interface {interface} "$@" --action del
    """
    unbind_script_filename = os.path.join(session_output_dir, "unbind.sh")
    with open(unbind_script_filename, "w") as f:
        f.write(unbind_script_content)
    os.chmod(unbind_script_filename, 0o755) # Делаем файл исполняемым
    print(f"Скрипт отвязки IPv6: {unbind_script_filename}")
    # *****************************************************************

    # Извлекаем и сохраняем прокси в отдельный файл
    extracted_proxy_filename = os.path.join(session_output_dir, "extracted_proxy")
    extract_proxies_from_content(full_proxy_content, extracted_proxy_filename, proxy_username, proxy_password)

    # Обновляем состояние после генерации всех прокси
    state[external_ipv4]["latest_port"] = current_port - 1
    state[external_ipv4]["ipv6_subnets"][current_ipv6_base_network_str]["latest_suffix_increment"] = current_ipv6_suffix_increment
    save_state(state)

    print(f"Сгенерировано {generated_count} прокси и их учетные данные в папке: {session_output_dir}")
    print(f"Основной конфиг: {full_config_filename}")
    print(f"Учетные данные: {credentials_output_filename}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Генератор конфигураций 3proxy.")
    parser.add_argument(
        "num_proxies",
        type=int,
        nargs="?", # Сделать необязательным
        help="Количество прокси для генерации."
    )
    parser.add_argument(
        "project_name",
        type=str,
        nargs="?", # Сделать необязательным
        help="Имя проекта (используется для поддиректории и имени файла конфига)."
    )
    parser.add_argument(
        "--ipv6-subnet",
        type=str,
        help="IPv6 подсеть для использования (например, 2a03:a03:a03:a03::/48 или 2a03:a03:a03:a03::/64).",
        default=None # По умолчанию None, чтобы можно было запросить интерактивно
    )
    parser.add_argument(
        "--interface",
        type=str,
        help="Сетевой интерфейс для привязки (например, ens3 или eth0).",
        default=None # По умолчанию None, чтобы можно было запросить интерактивно
    )
    parser.add_argument(
        "--external-ipv4",
        type=str,
        help="Внешний IPv4-адрес сервера (например, 192.168.1.1).",
        default=None # По умолчанию None, чтобы можно было запросить интерактивно
    )
    args = parser.parse_args()

    # Проверяем, предоставлены ли аргументы через командную строку, иначе запрашиваем
    num_proxies_input = args.num_proxies
    project_name_input = args.project_name
    ipv6_subnet_input = args.ipv6_subnet
    interface_input = args.interface
    external_ipv4_input = args.external_ipv4

    # Если количество прокси не было предоставлено, запрашиваем у пользователя
    while num_proxies_input is None:
        try:
            num_proxies_str = input("Пожалуйста, введите количество прокси для генерации: ")
            num_proxies_input = int(num_proxies_str)
            if num_proxies_input <= 0:
                print("Количество прокси должно быть положительным числом.", file=sys.stderr)
                num_proxies_input = None # Сбросить, чтобы запросить снова
        except ValueError:
            print("Некорректный ввод. Пожалуйста, введите целое число для количества прокси.", file=sys.stderr)

    # Если имя проекта не было предоставлено, запрашиваем у пользователя
    while project_name_input is None or not project_name_input.strip():
        project_name_input = input("Пожалуйста, введите имя проекта: ")
        if not project_name_input.strip():
            print("Имя проекта не может быть пустым.", file=sys.stderr)
            project_name_input = None # Сбросить, чтобы запросить снова

    # Если подсеть не была предоставлена, запрашиваем у пользователя
    while ipv6_subnet_input is None or not ipv6_subnet_input.strip():
        ipv6_subnet_input = input("Пожалуйста, введите IPv6 подсеть (например, 2a03:a03:a03:a03::/48 или 2a03:a03:a03:a03::/64): ")
        if not ipv6_subnet_input.strip():
            print("IPv6 подсеть не может быть пустой.", file=sys.stderr)

    # Если сетевой интерфейс не был предоставлен, запрашиваем у пользователя
    while interface_input is None or not interface_input.strip():
        interface_input = input("Пожалуйста, введите сетевой интерфейс для привязки (например, ens3 или eth0): ")
        if not interface_input.strip():
            print("Сетевой интерфейс не может быть пустым.", file=sys.stderr)
    
    # Если внешний IPv4 не был предоставлен, запрашиваем у пользователя
    while external_ipv4_input is None:
        try:
            input_ipv4 = input("Пожалуйста, введите внешний IPv4-адрес сервера: ")
            validate_ipv4(input_ipv4) # Проверяем формат
            external_ipv4_input = input_ipv4
        except ValueError as e:
            print(f"Ошибка ввода IPv4: {e}. Пожалуйста, введите корректный IPv4-адрес.", file=sys.stderr)
            external_ipv4_input = None # Сбросить, чтобы запросить снова

    # Убедимся, что базовая директория для генерируемых конфигов существует
    os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)

    generate_proxy_configs(
        num_proxies=num_proxies_input,
        project_name=project_name_input,
        ipv6_subnet=ipv6_subnet_input,
        interface=interface_input,
        external_ipv4=external_ipv4_input # Передаем внешний IPv4
    )