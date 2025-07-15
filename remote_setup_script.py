# -*- coding: utf-8 -*-
import paramiko
import time
import os
import sys
import getpass # Добавляем импорт getpass

def run_remote_command(hostname, username, password=None, key_filepath=None, command=None, sudo_password=None):
    """
    Выполняет команду на удаленном сервере по SSH.

    Args:
        hostname (str): IP-адрес или доменное имя удаленного сервера.
        username (str): Имя пользователя для SSH.
        password (str): Пароль для SSH (если используется аутентификация по паролю).
        key_filepath (str): Путь к приватному SSH-ключу (если используется аутентификация по ключу).
        command (str): Команда, которую нужно выполнить на удаленном сервере.
        sudo_password (str): Пароль для sudo (если команда требует sudo).

    Returns:
        tuple: Кортеж из стандартного вывода (stdout) и стандартного вывода ошибок (stderr).
    """

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        if password:
            client.connect(hostname=hostname, username=username, password=password, timeout=10)
        elif key_filepath:
            client.connect(hostname=hostname, username=username, key_filename=key_filepath, timeout=10)
        else:
            raise ValueError("Необходимо предоставить либо пароль, либо путь к SSH-ключу.")

        print(f"Подключение к {hostname} установлено.")

        if command:
            print(f"Выполнение команды: {command}")
            stdin, stdout, stderr = client.exec_command(command, get_pty=True)

            if "sudo" in command.lower() and sudo_password:
                stdin.write(sudo_password + '\n')
                stdin.flush()
            
            # Чтение вывода с небольшой задержкой, чтобы избежать обрезки больших результатов
            output = ""
            error = ""
            while True:
                line = stdout.readline()
                if not line:
                    break
                output += line
            while True:
                line = stderr.readline()
                if not line:
                    break
                error += line

            print(f"STDOUT:\n{output}")
            if error:
                print(f"STDERR:\n{error}")
            return output, error
        else:
            print("Команда не предоставлена для выполнения.")
            return "", ""

    except paramiko.AuthenticationException:
        print("Ошибка аутентификации. Проверьте учетные данные.")
        return "", "Authentication failed"
    except paramiko.SSHException as e:
        print(f"SSH-ошибка: {e}")
        return "", f"SSH error: {e}"
    except Exception as e:
        print(f"Произошла ошибка: {e}")
        return "", f"General error: {e}"
    finally:
        if client:
            client.close()
            print("SSH-соединение закрыто.")

def download_file_sftp(hostname, username, password=None, key_filepath=None, remote_path=None, local_path=None):
    """
    Скачивает файл с удаленного сервера по SFTP.

    Args:
        hostname (str): IP-адрес или доменное имя удаленного сервера.
        username (str): Имя пользователя для SSH.
        password (str): Пароль для SSH (если используется аутентификация по паролю).
        key_filepath (str): Путь к приватному SSH-ключу (если используется аутентификация по ключу).
        remote_path (str): Путь к файлу на удаленном сервере.
        local_path (str): Путь для сохранения файла на локальной машине.
    """
    transport = None
    sftp = None
    try:
        transport = paramiko.Transport((hostname, 22))
        if password:
            transport.connect(username=username, password=password)
        elif key_filepath:
            key = paramiko.RSAKey.from_private_key_file(key_filepath)
            transport.connect(username=username, pkey=key)
        else:
            raise ValueError("Необходимо предоставить либо пароль, либо путь к SSH-ключу.")

        sftp = paramiko.SFTPClient.from_transport(transport)
        print(f"Скачивание файла с {remote_path} на {local_path}...")
        sftp.get(remote_path, local_path)
        print(f"Файл успешно скачан: {local_path}")
    except FileNotFoundError:
        print(f"Ошибка: Удаленный файл не найден по пути: {remote_path}")
    except paramiko.AuthenticationException:
        print("Ошибка аутентификации. Проверьте учетные данные.")
    except paramiko.SSHException as e:
        print(f"SSH-ошибка при скачивании файла: {e}")
    except Exception as e:
        print(f"Произошла ошибка при скачивании файла: {e}")
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()


if __name__ == "__main__":
    # --- НАСТРОЙКИ ПОДКЛЮЧЕНИЯ ---
    REMOTE_HOST = input(f"Введите IP-адрес удаленного сервера: ")
    REMOTE_USER = input(f"Введите имя пользователя для SSH (по умолчанию: root): ") or "root"
    AUTH_PASSWORD = getpass.getpass(f"Введите пароль для SSH / Sudo (не будет отображаться): ")
    SUDO_PASSWORD = AUTH_PASSWORD
    AUTH_KEY_FILEPATH = None # Пока не используем SSH-ключ для упрощения

    DEFAULT_GIT_REPO_URL = "https://github.com/petrovichest/3proxy_configs_pub.git"
    DEFAULT_GIT_CLONE_DESTINATION = "/home"

    # --- НАСТРОЙКИ GIT КЛОНИРОВАНИЯ ---
    GIT_REPO_URL = input(f"Введите URL Git репозитория (по умолчанию: {DEFAULT_GIT_REPO_URL}): ") or DEFAULT_GIT_REPO_URL
    GIT_CLONE_DESTINATION = input(f"Введите путь для клонирования Git репозитория на удаленном сервере (по умолчанию: {DEFAULT_GIT_CLONE_DESTINATION}): ") or DEFAULT_GIT_CLONE_DESTINATION

    # Извлекаем имя репозитория из URL для определения конечной папки, созданной git clone
    REPO_NAME = GIT_REPO_URL.split('/')[-1]
    if REPO_NAME.endswith('.git'):
        REPO_NAME = REPO_NAME[:-4]
    ACTUAL_CLONE_DIR = os.path.join(GIT_CLONE_DESTINATION, REPO_NAME)

    # --- ЗАПРОС ПАРАМЕТРОВ ДЛЯ 1_generate_proxy_configs.py ---
    print("\n--- Введите параметры для генерации прокси ---")
    num_proxies_input = ""
    while not num_proxies_input.isdigit() or int(num_proxies_input) <= 0:
        num_proxies_input = input("Количество прокси для генерации (целое положительное число): ")
        if not num_proxies_input.isdigit() or int(num_proxies_input) <= 0:
            print("Некорректный ввод. Пожалуйста, введите целое положительное число.")
    num_proxies_input = int(num_proxies_input)

    project_name_input = ""
    while not project_name_input.strip():
        project_name_input = input("Имя проекта: ")
        if not project_name_input.strip():
            print("Имя проекта не может быть пустым.")
    
    ipv6_subnet_input = ""
    while not ipv6_subnet_input.strip():
        ipv6_subnet_input = input("IPv6 подсеть (например, 2a03:a03:a03::/48 или 2a03:a03:a03:a03::/64): ")
        if not ipv6_subnet_input.strip():
            print("IPv6 подсеть не может быть пустой.")

    interface_input = ""
    while not interface_input.strip():
        interface_input = input("Сетевой интерфейс для привязки (например, ens3 или eth0): ")
        if not interface_input.strip():
            print("Сетевой интерфейс не может быть пустым.")

    external_ipv4_input = REMOTE_HOST
    print(f"Внешний IPv4-адрес сервера: {external_ipv4_input} (взято из REMOTE_HOST)")

    # Формируем аргументы для run_generator.sh
    generator_params_for_script = (
        f"{num_proxies_input} "
        f"{project_name_input} "
        f"--ipv6-subnet {ipv6_subnet_input} "
        f"--interface {interface_input} "
        f"--external-ipv4 {external_ipv4_input}"
    )

    # --- ВЫПОЛНЕНИЕ ПОСЛЕДОВАТЕЛЬНОСТИ УСТАНОВКИ И ЗАПУСКА ---
    print("\n--- Выполнение последовательности установки и запуска ---")

    # 1. sudo apt update
    print("\n--- Выполнение 'sudo apt update' ---")
    stdout, stderr = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command="sudo apt update",
        sudo_password=SUDO_PASSWORD
    )
    if stderr:
        print("Ошибка при выполнении 'apt update'. Проверьте stderr выше.")
        # Добавляем выход из скрипта при критической ошибке
        sys.exit(1)

    # 2. sudo apt install git
    print("\n--- Выполнение 'sudo apt install git -y' ---")
    stdout, stderr = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command="sudo apt install git -y",
        sudo_password=SUDO_PASSWORD
    )
    if stderr:
        print("Ошибка при установке 'git'. Проверьте stderr выше.")
        sys.exit(1)

    # 3. Клонирование или обновление Git репозитория
    if GIT_REPO_URL and GIT_CLONE_DESTINATION:
        repo_url_for_clone = GIT_REPO_URL
        
        # Проверяем, существует ли директория репозитория на удаленной машине
        check_dir_command = f"test -d {ACTUAL_CLONE_DIR} && echo 'exists'"
        stdout_check, stderr_check = run_remote_command(
            hostname=REMOTE_HOST,
            username=REMOTE_USER,
            password=AUTH_PASSWORD,
            key_filepath=AUTH_KEY_FILEPATH,
            command=check_dir_command,
            sudo_password=SUDO_PASSWORD
        )

        if "exists" in stdout_check:
            print(f"\n--- Репозиторий {ACTUAL_CLONE_DIR} уже существует. Выполняю git pull ---")
            stdout, stderr = run_remote_command(
                hostname=REMOTE_HOST,
                username=REMOTE_USER,
                password=AUTH_PASSWORD,
                key_filepath=AUTH_KEY_FILEPATH,
                command=f"cd {ACTUAL_CLONE_DIR} && git pull",
                sudo_password=SUDO_PASSWORD
            )
            if stderr:
                print("Ошибка при выполнении git pull. Проверьте stderr выше.")
                sys.exit(1)
        else:
            print(f"\n--- Клонирование репозитория {repo_url_for_clone} в {ACTUAL_CLONE_DIR} ---")
            stdout, stderr = run_remote_command(
                hostname=REMOTE_HOST,
                username=REMOTE_USER,
                password=AUTH_PASSWORD,
                key_filepath=AUTH_KEY_FILEPATH,
                command=f"git clone {repo_url_for_clone} {ACTUAL_CLONE_DIR}",
                sudo_password=SUDO_PASSWORD
            )
            if stderr and "already exists" not in stderr: # "already exists" не будет ошибкой здесь, т.к. мы уже проверили
                print("Ошибка при клонировании репозитория. Проверьте stderr выше.")
                sys.exit(1)
    else:
        print("\n--- Пропущен шаг клонирования репозитория: не указан URL или путь для клонирования ---")
        sys.exit(1)
    
    # Запуск install_all.sh
    print(f"\n--- Запуск install_all.sh в {ACTUAL_CLONE_DIR} ---")
    stdout, stderr = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command=f"cd {ACTUAL_CLONE_DIR} && bash install_all.sh",
        sudo_password=SUDO_PASSWORD
    )
    if stderr:
        print("Ошибка при запуске install_all.sh. Проверьте stderr выше.")
        sys.exit(1)
    
    # Запуск run_generator.sh
    print(f"\n--- Запуск run_generator.sh в {ACTUAL_CLONE_DIR} с параметрами: {generator_params_for_script} ---")
    run_command = f"cd {ACTUAL_CLONE_DIR} && sudo bash run_generator.sh {generator_params_for_script}"
    stdout, stderr = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command=run_command,
        sudo_password=SUDO_PASSWORD
    )
    if stderr:
        print("Ошибка при запуске run_generator.sh. Проверьте stderr выше.")
        sys.exit(1)
    
    # Скачивание extracted_proxy
    print(f"\n--- Скачивание extracted_proxy ---")
    extracted_proxy_remote_path = os.path.join(ACTUAL_CLONE_DIR, f"generated_proxy_configs/{project_name_input}/extracted_proxy")
    local_output_dir = "downloaded_configs"
    os.makedirs(local_output_dir, exist_ok=True)
    extracted_proxy_local_path = os.path.join(local_output_dir, f"{project_name_input}")

    download_file_sftp(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        remote_path=extracted_proxy_remote_path,
        local_path=extracted_proxy_local_path
    )

    # Выполнение start_systemctl.sh
    print(f"\n--- Запуск start_systemctl.sh ---")
    start_systemctl_remote_path = os.path.join(ACTUAL_CLONE_DIR, f"generated_proxy_configs/{project_name_input}/start_systemctl.sh")
    start_systemctl_dir = os.path.dirname(start_systemctl_remote_path)
    start_systemctl_name = os.path.basename(start_systemctl_remote_path)
    stdout, stderr = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command=f"cd {start_systemctl_dir} && sudo bash {start_systemctl_name}",
        sudo_password=SUDO_PASSWORD
    )
    if stderr:
        print("Ошибка при запуске start_systemctl.sh. Проверьте stderr выше.")
        sys.exit(1)

    # Выполнение proxy_checker.sh для генерации файла результатов
    print(f"\n--- Запуск proxy_checker.sh для генерации файла результатов ---")
    proxy_checker_script_remote_path = os.path.join(ACTUAL_CLONE_DIR, f"generated_proxy_configs/{project_name_input}/proxy_checker.sh")
    proxy_checker_dir = os.path.dirname(proxy_checker_script_remote_path)
    proxy_checker_name = os.path.basename(proxy_checker_script_remote_path)
    stdout_checker_run, stderr_checker_run = run_remote_command(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        command=f"cd {proxy_checker_dir} && bash {proxy_checker_name}",
        sudo_password=SUDO_PASSWORD
    )

    if stderr_checker_run:
        print("Ошибка при запуске proxy_checker.sh. Проверьте stderr выше.")
        sys.exit(1)
    
    # Скачиваем файл proxy_check_results.txt
    print(f"\n--- Скачивание proxy_check_results.txt ---")
    proxy_results_remote_path = os.path.join(ACTUAL_CLONE_DIR, f"generated_proxy_configs/{project_name_input}/proxy_check_results.txt")
    proxy_results_local_path = os.path.join(local_output_dir, f"{project_name_input}_proxy_check_results.txt")

    download_file_sftp(
        hostname=REMOTE_HOST,
        username=REMOTE_USER,
        password=AUTH_PASSWORD,
        key_filepath=AUTH_KEY_FILEPATH,
        remote_path=proxy_results_remote_path,
        local_path=proxy_results_local_path
    )
    print(f"Результаты проверки прокси сохранены в: {proxy_results_local_path}")
    
