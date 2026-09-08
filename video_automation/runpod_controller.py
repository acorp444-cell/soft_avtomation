"""
Модуль управления подом RunPod через официальный GraphQL API.
Не зависит от конкретного ID пода - всегда находит самый свежий
(по дате создания) под на аккаунте, даже после миграций.

ИСПОЛЬЗОВАНИЕ (для проверки из командной строки):
    set RUNPOD_API_KEY=твой_ключ
    python runpod_controller.py status
    python runpod_controller.py start
    python runpod_controller.py stop

ТРЕБОВАНИЯ:
    pip install requests
"""

import os
import sys
import time
import requests

GRAPHQL_URL = "https://api.runpod.io/graphql"
POLL_EVERY_SEC = 5
START_TIMEOUT_SEC = 300  # 5 минут максимум на запуск пода


def _graphql(api_key: str, query: str, variables: dict = None) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        params={"api_key": api_key},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Ошибка RunPod API: {data['errors']}")
    return data["data"]


def get_all_pods(api_key: str) -> list:
    """Возвращает список всех подов на аккаунте с их статусом и SSH-портами."""
    query = """
    query {
      myself {
        pods {
          id
          name
          desiredStatus
          createdAt
          lastStartedAt
          machine {
            podHostId
          }
          runtime {
            ports {
              ip
              isIpPublic
              privatePort
              publicPort
              type
            }
          }
        }
      }
    }
    """
    data = _graphql(api_key, query)
    return data["myself"]["pods"]


def get_account_balance(api_key: str) -> float:
    """Возвращает текущий баланс аккаунта RunPod в долларах."""
    query = """
    query {
      myself {
        clientBalance
      }
    }
    """
    data = _graphql(api_key, query)
    return data["myself"]["clientBalance"]


def get_latest_pod(api_key: str) -> dict:
    """Находит самый свежий (по дате создания) под на аккаунте."""
    pods = get_all_pods(api_key)
    if not pods:
        raise RuntimeError("На аккаунте RunPod не найдено ни одного пода")
    latest = max(pods, key=lambda p: p["createdAt"])
    return latest


def resume_pod(api_key: str, pod_id: str, gpu_count: int = 1):
    """Запускает (возобновляет) под."""
    query = """
    mutation resumePod($input: PodResumeInput!) {
      podResume(input: $input) {
        id
        desiredStatus
      }
    }
    """
    variables = {"input": {"podId": pod_id, "gpuCount": gpu_count}}
    return _graphql(api_key, query, variables)


def stop_pod(api_key: str, pod_id: str):
    """Останавливает под (данные на диске сохраняются, GPU перестаёт тарифицироваться)."""
    query = """
    mutation stopPod($input: PodStopInput!) {
      podStop(input: $input) {
        id
        desiredStatus
      }
    }
    """
    variables = {"input": {"podId": pod_id}}
    return _graphql(api_key, query, variables)


def get_ssh_username(pod: dict):
    """Строит username для подключения через прокси ssh.runpod.io
    (формат 'podid-hostid'). Возвращает None, если podHostId недоступен.
    Устойчива к тому, что RunPod API иногда отдаёт podHostId уже как
    полную строку 'podid-hostid', а иногда только как 'hostid' (хвостик)."""
    machine = pod.get("machine") or {}
    pod_host_id = machine.get("podHostId")
    if not pod_host_id:
        return None
    if pod_host_id.startswith(pod["id"] + "-"):
        return pod_host_id  # уже полная строка, ID пода дублировать не нужно
    return f"{pod['id']}-{pod_host_id}"


def get_ssh_proxy_command(pod: dict):
    """Строит полную команду подключения через управляемый прокси
    ssh.runpod.io (использует SSH-ключ, привязанный к аккаунту, а не к
    конкретному поду - надёжнее, чем прямой TCP-адрес)."""
    username = get_ssh_username(pod)
    if not username:
        return None
    return f"ssh {username}@ssh.runpod.io -i ~/.ssh/id_ed25519"


def get_ssh_connection_info(pod: dict):
    """Извлекает публичный IP и порт для SSH (приватный порт 22, ПРОТОКОЛ TCP)
    из данных пода. RunPod отдаёт для одного и того же приватного порта
    ОТДЕЛЬНЫЕ tcp- и udp- записи с разными publicPort - SSH работает
    только по TCP, поэтому явно фильтруем по типу, иначе можно случайно
    получить udp-порт, на который SSH никогда не подключится.
    Возвращает (ip, port) или (None, None), если под ещё не готов."""
    runtime = pod.get("runtime")
    if not runtime:
        return None, None
    ports = runtime.get("ports") or []
    for p in ports:
        if (p.get("privatePort") == 22 and p.get("isIpPublic")
                and str(p.get("type", "")).lower() == "tcp"):
            return p.get("ip"), p.get("publicPort")
    return None, None


def wait_until_ready(api_key: str, pod_id: str, timeout_sec: int = START_TIMEOUT_SEC):
    """Ждёт, пока под запустится и получит SSH-адрес. Возвращает (ip, port)."""
    started = time.time()
    while time.time() - started < timeout_sec:
        pods = get_all_pods(api_key)
        pod = next((p for p in pods if p["id"] == pod_id), None)
        if pod is None:
            raise RuntimeError(f"Под {pod_id} не найден на аккаунте")

        ip, port = get_ssh_connection_info(pod)
        if ip and port:
            return ip, port

        print(f"  ...под запускается, статус: {pod.get('desiredStatus')}, "
              f"жду {POLL_EVERY_SEC} сек")
        time.sleep(POLL_EVERY_SEC)

    raise TimeoutError(f"Под не стал доступен по SSH за {timeout_sec} секунд")


def wait_until_stopped(api_key: str, pod_id: str, timeout_sec: int = START_TIMEOUT_SEC, on_progress=None):
    """Ждёт, пока под реально остановится (desiredStatus сменится с RUNNING)."""
    started = time.time()
    while time.time() - started < timeout_sec:
        pods = get_all_pods(api_key)
        pod = next((p for p in pods if p["id"] == pod_id), None)
        if pod is None:
            raise RuntimeError(f"Под {pod_id} не найден на аккаунте")

        status = pod.get("desiredStatus")
        if status != "RUNNING":
            return status

        msg = f"  ...под ещё останавливается, статус: {status}, жду {POLL_EVERY_SEC} сек"
        if on_progress:
            on_progress(msg)
        else:
            print(msg)
        time.sleep(POLL_EVERY_SEC)

    raise TimeoutError(f"Под не остановился за {timeout_sec} секунд")


# ---------- командная строка для проверки ----------

def main():
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан RUNPOD_API_KEY")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Использование: python runpod_controller.py [status|start|stop]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "status":
        pod = get_latest_pod(api_key)
        print(f"Актуальный под: {pod['name']} (id: {pod['id']})")
        print(f"Статус: {pod['desiredStatus']}")
        print(f"Создан: {pod['createdAt']}")

        raw_host_id = (pod.get("machine") or {}).get("podHostId")
        print(f"(отладка) сырое значение podHostId: {raw_host_id!r}")

        proxy_cmd = get_ssh_proxy_command(pod)
        if proxy_cmd:
            print(f"SSH (через прокси, надёжнее): {proxy_cmd}")
        else:
            print("SSH через прокси: podHostId не найден в ответе API")

        ip, port = get_ssh_connection_info(pod)
        if ip:
            print(f"SSH (напрямую по IP): ssh root@{ip} -p {port} -i ~/.ssh/id_ed25519")
        else:
            print("SSH напрямую пока недоступен (под остановлен или ещё запускается)")

    elif command == "start":
        pod = get_latest_pod(api_key)
        print(f"Запускаю под: {pod['name']} (id: {pod['id']})...")
        resume_pod(api_key, pod["id"])
        print("Команда на запуск отправлена, жду готовности...")
        ip, port = wait_until_ready(api_key, pod["id"])
        print(f"Готово! SSH: ssh root@{ip} -p {port} -i ~/.ssh/id_ed25519")

    elif command == "stop":
        pod = get_latest_pod(api_key)
        print(f"Останавливаю под: {pod['name']} (id: {pod['id']})...")
        stop_pod(api_key, pod["id"])
        print("Готово, под остановлен (GPU больше не тарифицируется).")

    else:
        print(f"Неизвестная команда: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
