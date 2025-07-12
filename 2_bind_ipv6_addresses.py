import argparse
import re
import subprocess
import os
from tqdm import tqdm
import sys # Добавляем импорт sys

# Примеры использования:
# Привязать IPv6-адреса:
# Извлечение IPv6-адресов:
# sudo python3 2_bind_ipv6_addresses.py MyProject --interface ens3 --action add
#
# Отвязка IPv6-адресов:
# sudo python3 proxy_configs/2_bind_ipv6_addresses.py MyProject --interface ens3 --action del

BASE_OUTPUT_DIR = "generated_proxy_configs"

def extract_ipv6_addresses(file_path):
    """
    Извлекает IPv6-адреса из файла proxy_configs.
    Ожидаемый формат строки: user:xxx pass:yyy proxy_ip:zzz proxy_port:ppp ipv6:AAAA:BBBB:CCCC:DDDD::N
    """
    ipv6_addresses = []
    ipv6_pattern = re.compile(r"ipv6:([0-9a-fA-F:]+)") # Захватываем полный IPv6-адрес
    try:
        with open(file_path, 'r') as f:
            for line in f:
                match = ipv6_pattern.search(line)
                if match:
                    ipv6_addresses.append(match.group(1)) # Теперь группа 1 содержит нужный префикс
    except FileNotFoundError:
        print(f"Ошибка: Файл не найден по пути {file_path}")
    return ipv6_addresses

def get_ipv6_command(ipv6_address, interface, action):
    """
    Возвращает строку команды для добавления или удаления IPv6-адреса.
    action: 'add' или 'del'
    """
    # Добавляем /64, так как 3proxy обычно использует /64 подсети, даже для отдельных IP
    # Более того, ip addr add требует CIDR
    # Команды будут выполняться через sudo, поэтому 'sudo' здесь не добавляем.
    return f"ip -6 addr {action} {ipv6_address}/64 dev {interface}"

def get_default_ipv6_interface():
    """
    Определяет имя сетевого интерфейса, который имеет глобальный IPv6-адрес
    или является интерфейсом по умолчанию для глобального IPv6-трафика.
    """
    try:
        # Попытка получить интерфейс по маршруту по умолчанию
        # Пример вывода: default via fe80::... dev eth0 proto ra metric 1024 expires 1783sec pref medium
        # Или: default dev eth0 proto ra metric 1024 pref medium
        result = subprocess.run(['ip', '-6', 'route', 'show', 'default'], capture_output=True, text=True, check=False)
        if result.returncode == 0 and "dev" in result.stdout:
            match = re.search(r"dev (\S+)", result.stdout)
            if match:
                return match.group(1)

        # Если маршрут по умолчанию не дал результата, ищем интерфейс с глобальным адресом
        # Пример вывода: 2: eth0    inet6 2a12:5940:d89b:x:y:z:a:b/64 scope global dynamic
        result = subprocess.run(['ip', '-6', 'address', 'show', 'scope', 'global'], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            if "scope global" in line.strip() and "inet6" in line.strip():
                parts = line.strip().split()
                # Предполагаем, что имя интерфейса находится во второй части после номера
                # Пример: 2: eth0    inet6 ...
                if len(parts) >= 2:
                    # Извлекаем имя интерфейса, отбрасывая двоеточие и число
                    interface_name_match = re.match(r"\d+:\s*(\S+)", parts[0])
                    if interface_name_match:
                        return interface_name_match.group(1)

        print("Предупреждение: Не удалось автоматически определить сетевой интерфейс IPv6. Используется 'ens3'.", file=sys.stderr)
        return "ens3" # Возвращаем ens3 в качестве запасного варианта, чтобы избежать сбоя

    except FileNotFoundError:
        print("Ошибка: Команда 'ip' не найдена. Убедитесь, что iproute2 установлен.", file=sys.stderr)
        return "ens3"
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при выполнении команды 'ip': {e.stderr}", file=sys.stderr)
        return "ens3"


def main():
    parser = argparse.ArgumentParser(description="Инструмент для привязки/отвязки IPv6-адресов к сетевому интерфейсу.")
    parser.add_argument("project_name", help="Имя проекта, содержащего файл proxy_configs.")
    parser.add_argument("--interface", default=None, help="Имя сетевого интерфейса. Если не указан, будет предпринята попытка автоматического определения.")
    parser.add_argument("--action", choices=["add", "del", "add_all"], default="add",
                        help="Действие: 'add' (добавить адреса), 'del' (удалить адреса) или 'add_all' (добавить все адреса для проекта). По умолчанию: add.")

    args = parser.parse_args()

    # Если действие 'add_all', то оно равносильно 'add'
    if args.action == "add_all":
        args.action = "add"

    # Если интерфейс не указан, пытаемся определить его автоматически
    if args.interface is None:
        detected_interface = get_default_ipv6_interface()
        if detected_interface:
            args.interface = detected_interface
            tqdm.write(f"Автоматически определен сетевой интерфейс IPv6: {args.interface}")
        else:
            tqdm.write("Ошибка: Не удалось определить сетевой интерфейс IPv6 автоматически. Пожалуйста, укажите его с помощью --interface.", file=sys.stderr)
            sys.exit(1) # Выход, если не удалось определить интерфейс


    # file_path = os.path.join(BASE_OUTPUT_DIR, args.project_name, "proxy_configs")
    # Так как скрипт bind.sh запускается из директории проекта, достаточно указать относительный путь
    file_path = "proxy_configs"

    # Проверяем, существует ли файл
    if not os.path.exists(file_path):
        tqdm.write(f"Ошибка: Указанный файл не существует: {file_path}")
        return

    ipv6_addresses = extract_ipv6_addresses(file_path)

    if not ipv6_addresses:
        tqdm.write(f"В файле {file_path} не найдено IPv6-адресов для обработки.")
        return

    tqdm.write(f"\nСобираю команды для {'привязки' if args.action == 'add' else 'отвязки'} IPv6-адресов...")
    commands_to_execute = []
    # Собираем команды в список, используя tqdm для отображения прогресса
    for ipv6 in tqdm(ipv6_addresses, desc="Сбор команд IPv6"):
        commands_to_execute.append(get_ipv6_command(ipv6, args.interface, args.action))

    tqdm.write(f"\nCобранно {len(commands_to_execute)} команд. Выполняю пакетную привязку/отвязку IPv6-адресов...")

    for cmd in tqdm(commands_to_execute, desc="Выполнение команд IPv6"):
        try:
            # Предупреждение: выполнение команд с sudo требует прав.
            # Если скрипт запускается без sudo, то эти команды будут требовать пароль.
            # Ожидается, что скрипт будет запущен с sudo.
            result = subprocess.run(f"sudo {cmd}", shell=True, capture_output=True, text=True, check=True)
            if result.stdout:
                # tqdm.write(f"Успех: {cmd.strip()} - {result.stdout.strip()}")
                pass # Пропускаем stdout, чтобы не засорять вывод, но можем включить для отладки
        except subprocess.CalledProcessError as e:
            tqdm.write(f"Ошибка выполнения команды: {cmd.strip()}")
            tqdm.write(f"Статус: {e.returncode}")
            tqdm.write(f"Ошибка stdout: {e.stdout}")
            tqdm.write(f"Ошибка stderr: {e.stderr}") # Вывод ошибки stderr
        except FileNotFoundError:
            tqdm.write("Ошибка: Команда 'ip' не найдена. Убедитесь, что iproute2 установлен.")


    tqdm.write("\nОперация завершена.")


if __name__ == "__main__":
    main()