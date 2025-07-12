import argparse
import asyncio
import os
import re
import aiohttp
from aiohttp import ClientError, ClientProxyConnectionError, ClientConnectionError, ClientResponseError, ClientTimeout
from tqdm.asyncio import tqdm

# Определение базовой директории для сгенерированных прокси
BASE_PROXY_CONFIGS_DIR = "generated_proxy_configs"
DEFAULT_CONCURRENCY = 50
DEFAULT_OUTPUT_FILENAME = "proxy_check_results.txt"
CHECK_URL = "http://ifconfig.me/ip"

async def check_proxy(proxy_info: dict, semaphore: asyncio.Semaphore, timeout: int = 10, current_check_url: str = CHECK_URL, is_retry: bool = False) -> tuple:
    """
    Асинхронно проверяет один прокси.
    Возвращает кортеж: (оригинальная_строка_прокси, статус_работоспособности, обнаруженный_IP, сообщение_об_ошибке)
    """
    proxy_string = proxy_info["original_string"]
    ip = proxy_info["ip"]
    port = proxy_info["port"]
    username = proxy_info["username"]
    password = proxy_info["password"]

    proxy_url = f"http://{username}:{password}@{ip}:{port}"

    try:
        async with semaphore:
            timeout_obj = ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_obj) as client:
                async with client.get(current_check_url, proxy=proxy_url) as response:
                    response.raise_for_status()  # Выбросит исключение для статусов 4xx/5xx
                    detected_ip = await response.text()

                detected_ip_stripped = detected_ip.strip()
                # Проверяем, является ли обнаруженный IP IPv4-адресом
                # Если proxy_info["ip"] - это IPv4, и ifconfig.me/ip возвращает IPv6, это означает, что прокси не был использован.
                return (proxy_string, True, detected_ip_stripped, "")
    except ClientProxyConnectionError as e:
        return (proxy_string, False, "", f"Ошибка прокси ({type(e).__name__}): {e}")
    except (ClientConnectionError, ConnectionRefusedError) as e:
        return (proxy_string, False, "", f"Ошибка подключения ({type(e).__name__}): {e}")
    except asyncio.TimeoutError as e:
        return (proxy_string, False, "", f"Таймаут в {timeout} секунд ({type(e).__name__})")
    except ClientResponseError as e:
        if e.status == 403 and not is_retry:
            print(f"Получена ошибка 403 для {proxy_string}, повторная попытка с https://ip6.me")
            # Повторная попытка с другим URL, указав, что это повторный запрос
            return await check_proxy(proxy_info, semaphore, timeout, "https://ip6.me", True)
        return (proxy_string, False, "", f"HTTP ошибка статуса: {e.status}, URL: {e.request_info.url if e.request_info else 'N/A'} ({type(e).__name__})")
    except ClientError as e:
        return (proxy_string, False, "", f"Ошибка клиента AIOHTTP ({type(e).__name__}): {e}")
    except Exception as e:
        return (proxy_string, False, "", f"Неизвестная ошибка ({type(e).__name__}): {e}")

def parse_proxy_line(line: str) -> dict or None:
    """Парсит строку прокси в словарь."""
    # Ожидаемый формат: IP:PORT@USERNAME:PASSWORD
    # Новый regex для поддержки IPv6 (с квадратными скобками и без), а также IPv4
    # Предполагается, что ip-адрес прокси будет соответствовать тому, что вернет CHECK_URL
    match = re.match(r"^\[?([0-9a-fA-F.:]+?)\]?:(\d+)@([^:]+):(.+)", line)
    if match:
        return {
            "original_string": line.strip(),
            "ip": match.group(1),
            "port": int(match.group(2)),
            "username": match.group(3),
            "password": match.group(4)
        }
    return None

async def load_proxies(project_name: str) -> list:
    """
    Загружает прокси из файла extracted_proxy, расположенного в директории проекта.
    """
    proxy_file_path = "extracted_proxy"
    proxies = []
    if not os.path.exists(proxy_file_path):
        print(f"Ошибка: Файл прокси не найден по пути: {proxy_file_path}")
        return []

    with open(proxy_file_path, 'r') as f:
        for line in f:
            proxy_data = parse_proxy_line(line)
            if proxy_data:
                proxies.append(proxy_data)
    print(f"Загружено {len(proxies)} прокси из {proxy_file_path}")
    return proxies

def write_results_to_file(results: list, output_filepath: str):
    """
    Записывает результаты проверки прокси в указанный файл.
    """
    with open(output_filepath, 'w') as f:
        for original_string, is_working, detected_ip, error_message in results:
            status = "РАБОТАЕТ" if is_working else "НЕ РАБОТАЕТ"
            output_line = f"{original_string} - {status}"
            if not is_working:
                if detected_ip:
                    output_line += f" (Фактический IP: {detected_ip}, Ошибка: {error_message})"
                else:
                    output_line += f" (Ошибка: {error_message})"
            f.write(output_line + "\n")
    print(f"Результаты проверки сохранены в: {output_filepath}")

async def main():
    parser = argparse.ArgumentParser(description="Прокси-чекер с асинхронной проверкой.")
    parser.add_argument(
        "--project-name",
        type=str,
        required=True,
        help="Имя проекта (название подпапки в generated_proxy_configs/, например 'mexc_5000')."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Количество одновременных запросов для проверки прокси (по умолчанию: {DEFAULT_CONCURRENCY})."
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=DEFAULT_OUTPUT_FILENAME,
        help=f"Имя файла для сохранения результатов (по умолчанию: {DEFAULT_OUTPUT_FILENAME})."
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Отключить отображение прогресс-бара."
    )
    args = parser.parse_args()

    project_name = args.project_name
    concurrency = args.concurrency
    output_file = args.output_file
    show_progress = not args.no_progress

    print(f"Запуск прокси-чекера для проекта '{project_name}' с параллелизмом {concurrency}...")

    proxies_to_check = await load_proxies(project_name)
    if not proxies_to_check:
        print("Нет прокси для проверки. Завершение работы.")
        return

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(check_proxy(proxy, semaphore)) for proxy in proxies_to_check]

    if show_progress:
        # Использование tqdm.asyncio.tqdm в качестве асинхронного контекстного менеджера
        # для отслеживания прогресса завершения задач.
        # Задачи уже созданы выше с asyncio.create_task.
        # await asyncio.gather(*tasks) будет ждать их завершения и собирать результаты.
        with tqdm(total=len(proxies_to_check), desc="Проверка прокси") as pbar:
            for f in asyncio.as_completed(tasks):
                await f
                pbar.update(1)
    results = await asyncio.gather(*tasks)

    write_results_to_file(results, output_file)
    print("Проверка прокси завершена.")

if __name__ == "__main__":
    asyncio.run(main())