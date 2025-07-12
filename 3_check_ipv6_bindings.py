import argparse
import re
import subprocess
import os
# Примеры использования:
# Проверить привязку IPv6-адресов:
# python3 3_check_ipv6_bindings.py MyProject --interface ens3

BASE_OUTPUT_DIR = "generated_proxy_configs"

def extract_ipv6_addresses(file_path):
    ipv6_addresses = []
    ipv6_pattern = re.compile(r"ipv6:([0-9a-fA-F:]+?)::2")
    try:
        with open(file_path, 'r') as f:
            for line in f:
                match = ipv6_pattern.search(line)
                if match:
                    ipv6_addresses.append(match.group(1))
    except FileNotFoundError:
        print(f"Ошибка: Файл не найден по пути {file_path}")
    return ipv6_addresses

def check_ipv6_binding(ipv6_address_prefix, interface):
    """
    Проверяет, привязан ли IPv6-адрес (с учетом ::/64) к указанному сетевому интерфейсу.
    """
    command = ['ip', '-6', 'addr', 'show', 'dev', interface]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        # Ищем полный IPv6-адрес в выводе, с учетом ::/64
        # ip output typically shows the full address including ::/64
        # Разделим префикс на сегменты и сформируем регулярное выражение для поиска
        # Учитываем, что в выводе `ip` ведущие нули в хекстетах могут отсутствовать (например, 03ea -> 3ea)
        segments = ipv6_address_prefix.split(':')
        regex_segments = []
        for seg in segments:
            if re.match(r"0[0-9a-fA-F]{1,3}", seg) and seg != "0": # Если сегмент начинается с нуля и не равен "0" (например, "03ea")
                regex_segments.append(f"({re.escape(seg)}|{re.escape(seg.lstrip('0'))})")
            else:
                regex_segments.append(re.escape(seg))
        
        # Собираем паттерн для поиска полного IPv6-адреса с учетом "::/64"
        # Ищем "inet6 <адрес>::/64"
        ipv6_regex_pattern_str = r"inet6\s+" + ":".join(regex_segments) + r":{1,2}/64"
        ipv6_regex_pattern = re.compile(ipv6_regex_pattern_str)
        
        if ipv6_regex_pattern.search(result.stdout):
            return True
        else:
            return False
    except subprocess.CalledProcessError as e:
        print(f"Ошибка выполнения команды 'ip' для интерфейса {interface}:")
        print(f"Статус: {e.returncode}")
        print(f"Ошибка: {e.stderr}")
        return False
    except FileNotFoundError:
        print("Ошибка: Команда 'ip' не найдена. Убедитесь, что iproute2 установлен.")
        return False

def main():
    parser = argparse.ArgumentParser(description="Проверка привязки IPv6-адресов к сетевому интерфейсу.")
    parser.add_argument("project_name", help="Имя проекта, содержащего файл proxy_configs.")
    parser.add_argument("--interface", default="ens3", help="Сетевой интерфейс для проверки (по умолчанию ens3).")
    args = parser.parse_args()

    file_path = os.path.join(BASE_OUTPUT_DIR, args.project_name, "proxy_configs")

    # Проверка существования файлаcredentials_file
    if not os.path.exists(file_path):
        print(f"Ошибка: Файл {file_path} не найден.")
        return

    ipv6_prefixes = extract_ipv6_addresses(file_path)

    if not ipv6_prefixes:
        print(f"В файле {file_path} не найдено IPv6-адресов для проверки.")
        return

    print(f"Проверка привязки IPv6-адресов на интерфейсе {args.interface}:")
    for prefix in ipv6_prefixes:
        if check_ipv6_binding(prefix, args.interface):
            print(f"  {prefix}::/64: ПРИВЯЗАН")
        else:
            print(f"  {prefix}::/64: НЕ ПРИВЯЗАН")

if __name__ == "__main__":
    main()