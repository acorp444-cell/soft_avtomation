"""
ГЛАВНОЕ ПРИЛОЖЕНИЕ - окно с кнопками для управления всем пайплайном
автоматизации видео (RunPod + генерация картинок/видео/озвучки/превью).

ПЕРВЫЙ ЗАПУСК:
    python -m pip install requests
    python app.py

Все ключи (RunPod, OpenAI, Lumean, RoyalTechno) вводятся один раз в
разделе "Настройки" и сохраняются локально на твоём компьютере в файле
video_automation_config.json (рядом с этой программой) - вводить их в
терминал заново каждый раз больше не нужно.

Файлы runpod_controller.py и ssh_runner.py должны лежать в той же папке.
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from runpod_controller import get_latest_pod, resume_pod, stop_pod, wait_until_ready, wait_until_stopped, get_account_balance
from ssh_runner import connect, run_command, get_pod_ssh_connection, download_file, upload_file, upload_directory, download_matching_files, list_remote_dirs, list_remote_files, cancel_all_local_transfers
from royaltechno_balance import get_royaltechno_balance
from ssh_key_setup import ensure_key_installed
from royaltechno_generate import generate_images, generate_videos_from_upscaled

CONFIG_PATH = Path(__file__).resolve().parent / "video_automation_config.json"
REMOTE_DIR = "/workspace/runpod-slim/ComfyUI/automation"
COMFYUI_INPUT_REMOTE_DIR = "/workspace/runpod-slim/ComfyUI/input"
COMFYUI_OUTPUT_REMOTE_DIR = "/workspace/runpod-slim/ComfyUI/output"
LOCAL_GENERATION_DIR = Path(__file__).resolve().parent / "local_generation"

DEFAULT_CONFIG = {
    "runpod_api_key": "",
    "openai_api_key": "",
    "openai_base_url": "",
    "lumean_api_key": "",
    "royaltechno_api_key": "",
    "lumean_template_id": "01a00ab2-3a8a-716c-b0f5-e205530b39d3",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Автоматизация видео — Заповедник Поневоле")
        self.geometry("1150x950")
        try:
            self.state("zoomed")  # разворачиваем на весь экран (Windows)
        except tk.TclError:
            pass

        self.config_data = load_config()
        self.output_queue = queue.Queue()
        self.ssh_client = None  # переиспользуем одно подключение между командами
        self.last_good_ip = None
        self.last_good_port = None
        self.cancel_start_event = threading.Event()

        # --- автовыключение сервера после завершения всех фоновых задач ---
        self.active_tasks_count = 0
        self.active_tasks_lock = threading.Lock()
        self.auto_shutdown_var = tk.BooleanVar(value=False)
        self.auto_shutdown_var.trace_add("write", lambda *a: self._update_auto_shutdown_label())
        self._shutdown_check_token = 0

        # --- отмена локальной генерации через RoyalTechno (без RunPod) ---
        self.local_gen_cancel_event = threading.Event()

        self._build_ui()
        self.after(100, self._poll_output_queue)
        self.after(200, self._set_initial_sash_position)

    def _set_initial_sash_position(self):
        """Даём журналу примерно 40% высоты окна при первом запуске
        (дальше пользователь может сам перетащить границу мышью)."""
        try:
            total_height = self.winfo_height()
            self.main_paned.sashpos(0, int(total_height * 0.48))
        except Exception:
            pass

    # ---------------- UI ----------------

    def _add_context_menu(self, entry_widget):
        """Добавляет меню 'Вставить/Копировать/Вырезать' по правому клику -
        на случай, если Ctrl+V почему-то не срабатывает."""
        menu = tk.Menu(entry_widget, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: entry_widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: entry_widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: entry_widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: entry_widget.select_range(0, "end"))

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)

        entry_widget.bind("<Button-3>", show_menu)
        return entry_widget

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_main = ttk.Frame(notebook)
        self.tab_settings = ttk.Frame(notebook)
        notebook.add(self.tab_main, text="Управление")
        notebook.add(self.tab_settings, text="Настройки")

        self._build_main_tab()
        self._build_settings_tab()

    def _update_auto_shutdown_label(self):
        if self.auto_shutdown_var.get():
            self.auto_shutdown_status_label.config(text="🟢 ВКЛ", foreground="#1a7f37")
        else:
            self.auto_shutdown_status_label.config(text="⚪ ВЫКЛ", foreground="#888888")

    def _build_main_tab(self):
        outer = self.tab_main

        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)
        self.main_paned = paned

        top_frame = ttk.Frame(paned)
        log_frame = ttk.LabelFrame(paned, text="Журнал")
        paned.add(top_frame, weight=1)
        paned.add(log_frame, weight=1)

        # Кнопок стало много, все сразу на экране могут не поместиться -
        # верхняя часть теперь прокручиваемая (колесо мыши или полоса
        # прокрутки справа), чтобы ничего не обрезалось и не пряталось.
        top_canvas = tk.Canvas(top_frame, highlightthickness=0)
        top_scrollbar = ttk.Scrollbar(top_frame, orient="vertical", command=top_canvas.yview)
        scrollable_frame = ttk.Frame(top_canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: top_canvas.configure(scrollregion=top_canvas.bbox("all")))
        canvas_window = top_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        top_canvas.configure(yscrollcommand=top_scrollbar.set)
        top_canvas.bind("<Configure>", lambda e: top_canvas.itemconfig(canvas_window, width=e.width))

        top_canvas.pack(side="left", fill="both", expand=True)
        top_scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            top_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        top_canvas.bind("<Enter>", lambda e: top_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        top_canvas.bind("<Leave>", lambda e: top_canvas.unbind_all("<MouseWheel>"))

        frame = scrollable_frame

        # --- блок управления сервером ---
        server_frame = ttk.LabelFrame(frame, text="Сервер RunPod")
        server_frame.pack(fill="x", padx=6, pady=6)

        self.status_label = ttk.Label(server_frame, text="Статус: не проверялся")
        self.status_label.pack(side="left", padx=6, pady=6)

        ttk.Button(server_frame, text="Статус", command=self.on_status).pack(side="left", padx=4)
        ttk.Button(server_frame, text="Включить сервер", command=self.on_start).pack(side="left", padx=4)
        ttk.Button(server_frame, text="🔑 Обновить ключ", command=self.on_refresh_key).pack(side="left", padx=4)
        ttk.Button(server_frame, text="Отмена", command=self.on_cancel_start).pack(side="left", padx=4)
        ttk.Button(server_frame, text="⛔ Остановить генерацию", command=self.on_stop_generation).pack(side="left", padx=4)
        ttk.Button(server_frame, text="Выключить сервер", command=self.on_stop).pack(side="left", padx=4)

        ttk.Checkbutton(server_frame, text="Автовыключение после завершения задач",
                         variable=self.auto_shutdown_var).pack(side="left", padx=(16, 2))
        self.auto_shutdown_status_label = ttk.Label(server_frame, text="")
        self.auto_shutdown_status_label.pack(side="left", padx=2)
        self._update_auto_shutdown_label()

        # --- блок баланса ---
        balance_frame = ttk.LabelFrame(frame, text="Баланс")
        balance_frame.pack(fill="x", padx=6, pady=6)

        self.balance_label = ttk.Label(balance_frame, text="RunPod: —    |    RoyalTechno: —")
        self.balance_label.pack(side="left", padx=6, pady=6)
        ttk.Button(balance_frame, text="Обновить баланс", command=self.on_refresh_balance).pack(side="left", padx=4)
        ttk.Label(balance_frame, text="(OpenAI баланс через API недоступен - смотри на platform.openai.com)",
                  foreground="#888888").pack(side="left", padx=10)

        # --- блок шагов пайплайна ---
        steps_frame = ttk.LabelFrame(frame, text="Шаги пайплайна")
        steps_frame.pack(fill="x", padx=6, pady=6)

        ttk.Label(steps_frame, text="Название сценария (папка внутри 'тексты блоков' и 'результаты'):").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 0))
        self.blocks_dir_var = tk.StringVar(value="тексты блоков")
        blocks_entry = ttk.Entry(steps_frame, textvariable=self.blocks_dir_var, width=40)
        blocks_entry.grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))
        self._add_context_menu(blocks_entry)

        buttons = [
            ("0. Разбить сценарий на блоки", self.on_split_script),
            ("0а. Загрузить тексты блоков", self.on_upload_blocks),
            ("0б. Загрузить файл (промт/библиотека)", self.on_upload_file),
            ("1. OBJECT_LIBRARY", self.on_generate_library),
            ("2. Озвучка + CSV", self.on_full_pipeline),
            ("3. Проверить CSV (длительность)", self.on_check_coverage),
            ("3б. Проверить CSV (текст)", self.on_check_voiceover_coverage),
            ("4. Картинки + видео", self.on_generate_media),
            ("5. Превью (текст)", self.on_thumbnails_dry),
            ("5б. Превью (картинки)", self.on_thumbnails_run),
            ("5в. Музыка (промт для Suno)", self.on_generate_music),
            ("6. Собрать архив на сервере", self.on_pack_output),
            ("7. Скачать результаты (архив)", self.on_download_output),
            ("8. Скачать один CSV", self.on_download_one_csv),
            ("9. Скачать все CSV", self.on_download_all_csv),
            ("10. Скачать папку (любую)", self.on_download_folder),
            ("11. Нарезать аудио по кадрам", self.on_split_audio),
            ("12. Собрать видео блока", self.on_assemble_video),
        ]
        for i, (label, handler) in enumerate(buttons):
            ttk.Button(steps_frame, text=label, command=handler, width=30).grid(
                row=2 + i // 3, column=i % 3, padx=6, pady=6, sticky="w")

        # --- параллельная генерация картинок/видео ---
        queues_row = ttk.Frame(frame)
        queues_row.pack(fill="both", expand=True, padx=6, pady=6)
        queues_row.grid_columnconfigure(0, weight=1)
        queues_row.grid_columnconfigure(1, weight=1)

        parallel_frame = ttk.LabelFrame(queues_row, text="Очередь генерации картинок/видео (макс. 3)")
        parallel_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        ttk.Label(parallel_frame, text="CSV через запятую:").grid(
            row=0, column=0, columnspan=2, padx=6, pady=(6, 0), sticky="w")
        self.parallel_csv_var = tk.StringVar()
        parallel_csv_entry = ttk.Entry(parallel_frame, textvariable=self.parallel_csv_var)
        parallel_csv_entry.grid(row=1, column=0, padx=6, pady=(0, 6), sticky="ew")
        self._add_context_menu(parallel_csv_entry)
        ttk.Button(parallel_frame, text="Добавить", command=self.on_add_to_queue).grid(
            row=1, column=1, padx=6, pady=(0, 6))
        parallel_frame.grid_columnconfigure(0, weight=1)

        # --- панель статуса очереди ---
        self.queue_tree = ttk.Treeview(parallel_frame, columns=("status",), show="tree headings", height=5)
        self.queue_tree.heading("#0", text="Блок")
        self.queue_tree.heading("status", text="Статус")
        self.queue_tree.column("#0", width=140)
        self.queue_tree.column("status", width=220)
        self.queue_tree.grid(row=2, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="nsew")

        ttk.Button(parallel_frame, text="Очистить завершённые",
                   command=self.on_clear_finished_queue).grid(row=3, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="w")

        self.gen_semaphore = threading.Semaphore(3)
        self.queue_blocks = set()  # какие блоки уже добавлены (не даём дублировать)

        # --- панель очереди сборки видео блоков ---
        # Лимит 1 (не 3, как у очереди генерации картинок/видео) - сборка
        # видео кодирует через ffmpeg на процессоре сервера, а не ждёт
        # ответа от внешнего API, поэтому несколько сборок одновременно
        # просто делят между собой одни и те же ядра CPU и работают
        # медленнее все вместе, чем одна за другой по очереди.
        assemble_frame = ttk.LabelFrame(queues_row, text="Очередь сборки видео блоков (макс. 1)")
        assemble_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        ttk.Label(assemble_frame, text="CSV через запятую:").grid(
            row=0, column=0, columnspan=2, padx=6, pady=(6, 0), sticky="w")
        self.assemble_csv_var = tk.StringVar()
        assemble_csv_entry = ttk.Entry(assemble_frame, textvariable=self.assemble_csv_var)
        assemble_csv_entry.grid(row=1, column=0, padx=6, pady=(0, 6), sticky="ew")
        self._add_context_menu(assemble_csv_entry)
        ttk.Button(assemble_frame, text="Добавить", command=self.on_add_to_assemble_queue).grid(
            row=1, column=1, padx=6, pady=(0, 6))
        assemble_frame.grid_columnconfigure(0, weight=1)

        self.assemble_tree = ttk.Treeview(assemble_frame, columns=("status",), show="tree headings", height=5)
        self.assemble_tree.heading("#0", text="Блок")
        self.assemble_tree.heading("status", text="Статус")
        self.assemble_tree.column("#0", width=140)
        self.assemble_tree.column("status", width=220)
        self.assemble_tree.grid(row=2, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="nsew")

        ttk.Button(assemble_frame, text="Очистить завершённые",
                   command=self.on_clear_finished_assemble_queue).grid(row=3, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="w")

        self.assemble_semaphore = threading.Semaphore(1)
        self.assemble_queue_blocks = set()

        # --- генерация через RoyalTechno без RunPod (экономия) ---
        economy_frame = ttk.LabelFrame(frame, text="Генерация через RoyalTechno без RunPod (экономия)")
        economy_frame.pack(fill="x", padx=6, pady=6)
        ttk.Label(economy_frame,
                  text="Порядок: A (на компьютере, RunPod можно выключить) -> B (коротко включить "
                       "RunPod для апскейла) -> C (на компьютере) -> D (снова коротко включить "
                       "RunPod, залить готовое).",
                  foreground="#888888", wraplength=1000).pack(anchor="w", padx=6, pady=(6, 2))
        econ_row = ttk.Frame(economy_frame)
        econ_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(econ_row, text="A. Картинки без RunPod", command=self.on_generate_images_local,
                   width=26).pack(side="left", padx=4)
        ttk.Button(econ_row, text="B. Апскейл на RunPod", command=self.on_upscale_on_runpod,
                   width=22).pack(side="left", padx=4)
        ttk.Button(econ_row, text="C. Видео без RunPod", command=self.on_generate_videos_local,
                   width=22).pack(side="left", padx=4)
        ttk.Button(econ_row, text="D. Залить готовое на RunPod", command=self.on_upload_generated_to_runpod,
                   width=28).pack(side="left", padx=4)

        # --- произвольная команда ---
        custom_frame = ttk.LabelFrame(frame, text="Своя команда (для гибкости)")
        custom_frame.pack(fill="x", padx=6, pady=6)
        self.custom_cmd_var = tk.StringVar()
        custom_entry = ttk.Entry(custom_frame, textvariable=self.custom_cmd_var, width=80)
        custom_entry.pack(side="left", padx=6, pady=6, fill="x", expand=True)
        self._add_context_menu(custom_entry)
        ttk.Button(custom_frame, text="Выполнить", command=self.on_custom_command).pack(side="left", padx=6)

        # --- лог вывода ---
        self.log_text = tk.Text(log_frame, bg="#1e1e1e", fg="#d4d4d4",
                                 insertbackground="white", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        scrollbar = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

    def _build_settings_tab(self):
        frame = self.tab_settings
        ttk.Label(frame, text="Ключи сохраняются локально на этом компьютере, "
                               "рядом с программой (video_automation_config.json).",
                  wraplength=600).pack(anchor="w", padx=10, pady=(10, 14))

        self.settings_vars = {}
        fields = [
            ("runpod_api_key", "RunPod API-ключ"),
            ("openai_api_key", "OpenAI (или агрегатор) API-ключ"),
            ("openai_base_url", "OpenAI base URL (необязательно, если не агрегатор)"),
            ("lumean_api_key", "Lumean API-ключ (озвучка)"),
            ("lumean_template_id", "Lumean template_id (голос)"),
            ("royaltechno_api_key", "RoyalTechno API-ключ (картинки/видео)"),
        ]
        for key, label in fields:
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=10, pady=4)
            ttk.Label(row, text=label, width=38).pack(side="left")
            var = tk.StringVar(value=self.config_data.get(key, ""))
            self.settings_vars[key] = var
            show = "*" if "key" in key else ""
            settings_entry = ttk.Entry(row, textvariable=var, width=50, show=show)
            settings_entry.pack(side="left", fill="x", expand=True)
            self._add_context_menu(settings_entry)

        ttk.Button(frame, text="Сохранить настройки", command=self.on_save_settings).pack(pady=16)

    # ---------------- вспомогательные ----------------

    def log(self, text: str):
        self.output_queue.put(text)

    def _poll_output_queue(self):
        try:
            while True:
                line = self.output_queue.get_nowait()
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_output_queue)

    def run_in_background(self, func, *args):
        with self.active_tasks_lock:
            self.active_tasks_count += 1

        def wrapper():
            try:
                func(*args)
            finally:
                with self.active_tasks_lock:
                    self.active_tasks_count -= 1
                    remaining = self.active_tasks_count
                if remaining == 0:
                    self.after(0, self._schedule_auto_shutdown_check)

        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

    def _schedule_auto_shutdown_check(self):
        """Вызывается (в основном потоке), когда все фоновые задачи закончились.
        Если включено автовыключение - ждём немного (вдруг сейчас начнётся
        следующий шаг очереди) и проверяем ещё раз перед реальным выключением."""
        if not self.auto_shutdown_var.get():
            return
        self._shutdown_check_token += 1
        token = self._shutdown_check_token
        self.log("\n[Автовыключение] Все текущие задачи завершены. Если за 15 секунд "
                  "не начнётся ничего нового - сервер выключится автоматически.\n")
        self.after(15000, lambda: self._maybe_auto_shutdown(token))

    def _maybe_auto_shutdown(self, token):
        if not self.auto_shutdown_var.get():
            return
        if token != self._shutdown_check_token:
            return  # за это время запустилась новая задача - эта проверка устарела
        with self.active_tasks_lock:
            still_idle = (self.active_tasks_count == 0)
        if not still_idle:
            return
        self.log("[Автовыключение] Задач по-прежнему нет - выключаю сервер...\n")
        self.auto_shutdown_var.set(False)  # выключаем галочку, чтобы не сработало повторно
        self.run_in_background(self._stop_task)

    def get_ssh_client(self):
        """Возвращает объект подключения (параметры ip/port/ключ) - само
        создание мгновенное, реальное сетевое обращение происходит только
        при выполнении команды/передаче файла (через системный ssh.exe)."""
        api_key = self.config_data.get("runpod_api_key")
        if not api_key:
            raise RuntimeError("Не задан RunPod API-ключ (вкладка Настройки)")

        # используем уже проверенный ip/port, если он есть - RunPod API
        # иногда отдаёт слегка расходящиеся данные при частых повторных
        # запросах, поэтому лишний раз не переспрашиваем
        if self.last_good_ip and self.last_good_port:
            ip, port = self.last_good_ip, self.last_good_port
        else:
            ip, port = get_pod_ssh_connection(api_key)
            self.last_good_ip, self.last_good_port = ip, port

        self.ssh_client = connect(ip, port)
        return self.ssh_client

    def ensure_connected_then(self, callback):
        """Подключается к серверу В ФОНЕ (с прогрессом в журнале), а затем
        вызывает callback уже на главном потоке - используется перед
        открытием диалоговых окошек (выбор папки/файла), чтобы окно
        программы не 'зависало' на время подключения."""
        def _task():
            try:
                self.get_ssh_client()
            except Exception as e:
                self.log(f"ОШИБКА подключения: {e}")
                return
            self.after(0, callback)
        self.run_in_background(_task)

    def capture_remote_output(self, command: str) -> str:
        """Выполняет команду и возвращает весь вывод как строку (для случаев,
        когда нужен сам результат, а не просто журнал)."""
        full_command = self._build_full_command(command)
        client = self.get_ssh_client()
        lines = []
        run_command(client, full_command, on_output=lines.append)
        return "\n".join(lines)

    def _build_full_command(self, command: str) -> str:
        cfg = self.config_data
        env_prefix = (
            f'export PYTHONUNBUFFERED=1 && '
            f'export OPENAI_API_KEY="{cfg.get("openai_api_key", "")}" && '
            f'export LUMEAN_API_KEY="{cfg.get("lumean_api_key", "")}" && '
            f'export ROYALTECHNO_API_KEY="{cfg.get("royaltechno_api_key", "")}" && '
        )
        if cfg.get("openai_base_url"):
            env_prefix += f'export OPENAI_BASE_URL="{cfg["openai_base_url"]}" && '

        # автоматически ставим openai, если после миграции пода библиотека
        # ещё не установлена - незаметно для пользователя
        ensure_openai = 'python3 -c "import openai" 2>/dev/null || pip install --quiet openai; '

        return f'cd {REMOTE_DIR} && {env_prefix}{ensure_openai}{command}'

    def exec_remote(self, command: str, prefix: str = ""):
        """Выполняет команду на сервере (с активированными переменными окружения) и логирует вывод."""
        full_command = self._build_full_command(command)

        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"{prefix}ОШИБКА подключения: {e}")
            return

        self.log(f"\n{prefix}>>> {command}\n")
        try:
            exit_code = run_command(client, full_command, on_output=lambda line: self.log(f"{prefix}{line}"))
            self.log(f"\n{prefix}[завершено, код: {exit_code}]\n")
        except Exception as e:
            self.log(f"{prefix}ОШИБКА выполнения: {e}")

    def exec_remote_own_connection(self, command: str, prefix: str = ""):
        """То же самое, что exec_remote, но открывает СВОЁ отдельное SSH-
        подключение вместо общего - используется для параллельных задач,
        чтобы они не мешали друг другу и основному подключению."""
        full_command = self._build_full_command(command)
        api_key = self.config_data.get("runpod_api_key")

        try:
            if self.last_good_ip and self.last_good_port:
                ip, port = self.last_good_ip, self.last_good_port
            else:
                ip, port = get_pod_ssh_connection(api_key)
                self.last_good_ip, self.last_good_port = ip, port
            client = connect(ip, port)
        except Exception as e:
            self.log(f"{prefix}ОШИБКА подключения: {e}")
            return

        self.log(f"\n{prefix}>>> {command}\n")
        try:
            exit_code = run_command(client, full_command, on_output=lambda line: self.log(f"{prefix}{line}"))
            self.log(f"\n{prefix}[завершено, код: {exit_code}]\n")
        except Exception as e:
            self.log(f"{prefix}ОШИБКА выполнения: {e}")

    # ---------------- обработчики кнопок: сервер ----------------

    def on_status(self):
        self.run_in_background(self._status_task)

    def _status_task(self):
        api_key = self.config_data.get("runpod_api_key")
        if not api_key:
            self.log("ОШИБКА: не задан RunPod API-ключ (вкладка Настройки)")
            return
        try:
            pod = get_latest_pod(api_key)
            status = pod["desiredStatus"]
            self.status_label.config(text=f"Статус: {status} ({pod['name']})")
            self.log(f"Под: {pod['name']} (id: {pod['id']}), статус: {status}")
        except Exception as e:
            self.log(f"ОШИБКА: {e}")

    def on_refresh_key(self):
        """Прописывает SSH-ключ на том поде, который сейчас реально запущен
        (например, если ты включила его вручную на сайте RunPod) - без
        попытки его 'включить' заново."""
        self.run_in_background(self._refresh_key_task)

    def _refresh_key_task(self):
        api_key = self.config_data.get("runpod_api_key")
        if not api_key:
            self.log("ОШИБКА: не задан RunPod API-ключ (вкладка Настройки)")
            return
        try:
            pod = get_latest_pod(api_key)
        except Exception as e:
            self.log(f"ОШИБКА: {e}")
            return

        if pod.get("desiredStatus") != "RUNNING":
            self.log(f"Под {pod['name']} сейчас не запущен (статус: {pod.get('desiredStatus')}). "
                      f"Сначала включи его - на сайте или кнопкой 'Включить сервер'.")
            return

        self.log(f"Под {pod['name']} уже запущен, прописываю SSH-ключ...")
        key_ok = ensure_key_installed(pod, on_output=self.log)
        if key_ok:
            self.status_label.config(text=f"Статус: RUNNING ({pod['name']})")
            self.ssh_client = None  # сбрасываем старое подключение, если было
            self.last_good_ip = None
            self.last_good_port = None
            self.log("Ключ прописан, жду 5 сек, чтобы прямое подключение стабилизировалось...")
            time.sleep(5)
            self.log("Готово! Можно работать.")
        else:
            self.log("ПРЕДУПРЕЖДЕНИЕ: не удалось подтвердить установку ключа.")

    def on_start(self):
        self.cancel_start_event.clear()
        self.run_in_background(self._start_task)

    def on_cancel_start(self):
        self.cancel_start_event.set()
        self.log("Отмена запрошена - остановлюсь на следующей проверке...")

    def on_stop_generation(self):
        if not messagebox.askyesno(
                "Подтверждение",
                "Принудительно остановить все скрипты генерации на сервере, а также "
                "генерацию через RoyalTechno на этом компьютере (шаги A/C)?\n"
                "Уже потраченные на текущий блок деньги не вернутся, но дальнейшая "
                "генерация прекратится."):
            return
        self.run_in_background(self._stop_generation_task)

    def _stop_generation_task(self):
        # останавливаем локальную генерацию через RoyalTechno (шаги A/C, без
        # RunPod) - она крутится в текущем процессе, не как отдельный скрипт,
        # поэтому убивается через флаг, а не через pkill
        self.local_gen_cancel_event.set()

        # локальные операции (скачивание/загрузку файлов) -
        # они выполняются на этом компьютере, серверный pkill их не видит
        local_stopped = cancel_all_local_transfers()
        if local_stopped:
            self.log(f"\n>>> Остановлено локальных операций (скачивание/загрузка): {local_stopped}\n")

        # известные скрипты пайплайна - завершаем все сразу, безопасно
        # (если какой-то не запущен - pkill просто ничего не найдёт, не ошибка)
        script_names = [
            "full_pipeline.py",
            "fix_and_renumber_pipeline.py",
            "generate_via_api_and_upscale.py",
            "upscale_batch.py",
            "generate_object_library.py",
            "generate_thumbnails.py",
            "generate_music_prompt.py",
            "generate_csv_from_text.py",
            "split_audio_by_csv.py",
            "assemble_block_video.py",
            "split_script_into_blocks.py",
            "ffmpeg",  # чтобы прервать и сам процесс кодирования видео, не только python-скрипт
        ]
        kill_cmd = " ; ".join(f'pkill -9 -f "{name}"' for name in script_names)
        self.log("\n>>> Останавливаю все запущенные скрипты генерации на сервере...\n")
        # используем отдельное подключение - основное может быть занято
        # чтением вывода уже идущей команды
        self.exec_remote_own_connection(kill_cmd, prefix="[стоп] ")
        self.log("Готово - если что-то было запущено (на сервере или локально), оно должно было прерваться.\n")

    def _start_task(self):
        api_key = self.config_data.get("runpod_api_key")
        if not api_key:
            self.log("ОШИБКА: не задан RunPod API-ключ (вкладка Настройки)")
            return
        try:
            pod = get_latest_pod(api_key)

            if pod.get("desiredStatus") == "RUNNING":
                self.log(f"Под {pod['name']} уже запущен - пропускаю попытки запуска, "
                          f"сразу прописываю ключ...")
            else:
                self.log(f"Запускаю под {pod['name']}...")

                max_attempts = 10
                retry_delay_sec = 30
                for attempt in range(1, max_attempts + 1):
                    if self.cancel_start_event.is_set():
                        self.log("Запуск отменён пользователем.")
                        return
                    try:
                        resume_pod(api_key, pod["id"])
                        break
                    except Exception as e:
                        error_text = str(e)
                        if "not enough free GPUs" in error_text or "GPUs are no longer available" in error_text:
                            self.log(f"  Свободных GPU на этой машине пока нет "
                                      f"(попытка {attempt}/{max_attempts}). "
                                      f"Жду {retry_delay_sec} сек и пробую снова...")
                            if attempt == max_attempts:
                                self.log("Не удалось запустить под - свободных GPU не нашлось за "
                                          "отведённое время. Попробуй ещё раз позже, или зайди "
                                          "на сайт RunPod и используй 'Automatically migrate'.")
                                return
                            if self.cancel_start_event.wait(retry_delay_sec):
                                self.log("Запуск отменён пользователем.")
                                return
                        else:
                            raise

            if self.cancel_start_event.is_set():
                self.log("Запуск отменён пользователем.")
                return

            self.log("Команда отправлена, жду готовности (это может занять минуту-две)...")
            ip, port = wait_until_ready(api_key, pod["id"])
            self.status_label.config(text=f"Статус: RUNNING ({pod['name']})")
            self.log(f"Под доступен: {ip}:{port}")

            self.log("Прописываю SSH-ключ на сервере (на случай миграции)...")
            # под мог обновить данные (например podHostId) - берём свежую копию
            fresh_pod = get_latest_pod(api_key)
            key_ok = ensure_key_installed(fresh_pod, on_output=self.log)
            if key_ok:
                self.log("Ключ прописан, жду 5 сек, чтобы прямое подключение стабилизировалось...")
                time.sleep(5)
                self.log("Готово! Можно работать.")
            else:
                self.log("ПРЕДУПРЕЖДЕНИЕ: не удалось подтвердить установку ключа - "
                          "прямое подключение может не сработать. Смотри журнал выше.")
        except Exception as e:
            self.log(f"ОШИБКА: {e}")

    def on_stop(self):
        if not messagebox.askyesno("Подтверждение", "Точно остановить сервер? "
                                                       "Убедись, что генерация сейчас не идёт."):
            return
        self.run_in_background(self._stop_task)

    def _stop_task(self):
        api_key = self.config_data.get("runpod_api_key")
        try:
            pod = get_latest_pod(api_key)
            self.log(f"Останавливаю под {pod['name']}...")
            result = stop_pod(api_key, pod["id"])
            self.log(f"Команда отправлена (ответ API: {result}), жду реальной остановки...")
            final_status = wait_until_stopped(api_key, pod["id"], on_progress=self.log)
            self.status_label.config(text=f"Статус: {final_status}")
            self.log(f"Готово, сервер остановлен (статус: {final_status}).")
            self.ssh_client = None
            self.last_good_ip = None
            self.last_good_port = None
        except Exception as e:
            self.log(f"ОШИБКА: {e}")

    # ---------------- обработчики кнопок: пайплайн ----------------

    def get_blocks_dir_or_warn(self):
        blocks_dir = self.blocks_dir_var.get().strip()
        if not blocks_dir:
            messagebox.showwarning("Не заполнено", "Впиши название папки со сценарием "
                                                      "в поле 'Название сценария' сверху.")
            return None
        return blocks_dir

    def on_generate_library(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        cmd = (f'python3 generate_object_library.py --blocks-dir "{blocks_dir}" '
               f'--master MASTER_ПРОМТ_OBJECT_LIBRARY.txt --output OBJECT_LIBRARY.md')
        self.run_in_background(self.exec_remote, cmd)

    def on_full_pipeline(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        template_id = self.config_data.get("lumean_template_id", "")
        cmd = (f'python3 full_pipeline.py --blocks-dir "{blocks_dir}" '
               f'--library OBJECT_LIBRARY.md --master VEO_3_МАСТЕР_ПРОМТ_КРЕАТИВ.txt '
               f'--template-id {template_id} --output-dir результаты --start-num 1')
        self.run_in_background(self.exec_remote, cmd)

    def on_check_coverage(self):
        cmd = 'python3 check_csv_coverage.py --dir результаты'
        self.run_in_background(self.exec_remote, cmd)

    def on_check_voiceover_coverage(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        cmd = f'python3 check_voiceover_coverage.py --texts-dir "{blocks_dir}" --csv-dir результаты'
        self.run_in_background(self.exec_remote, cmd)

    def on_generate_media(self):
        self.pick_remote_file_async(f"{REMOTE_DIR}/результаты", ".csv", "Выбери CSV для генерации",
                                     self._on_generate_media_picked)

    def _on_generate_media_picked(self, csv_name):
        if not csv_name:
            return
        if csv_name in self.queue_blocks:
            messagebox.showinfo("Уже в работе", f"{csv_name} уже есть в панели очереди - "
                                                   f"смотри там его статус.")
            return
        self.queue_blocks.add(csv_name)
        self.queue_tree.insert("", "end", iid=csv_name, text=csv_name, values=("⏳ В очереди",))
        self.run_in_background(self._run_queued_generation, csv_name)

    def on_thumbnails_dry(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        cmd = (f'python3 generate_thumbnails.py --blocks-dir "{blocks_dir}" '
               f'--library OBJECT_LIBRARY.md --titles-master MASTER_ПРОМТ_THUMBNAIL_TITLES.txt '
               f'--images-master MASTER_ПРОМТ_THUMBNAIL_IMAGES.txt --output-dir превью')
        self.run_in_background(self.exec_remote, cmd)

    def on_thumbnails_run(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        cmd = (f'python3 generate_thumbnails.py --blocks-dir "{blocks_dir}" '
               f'--library OBJECT_LIBRARY.md --titles-master MASTER_ПРОМТ_THUMBNAIL_TITLES.txt '
               f'--images-master MASTER_ПРОМТ_THUMBNAIL_IMAGES.txt --output-dir превью --run')
        self.run_in_background(self.exec_remote, cmd)

    def on_generate_music(self):
        blocks_dir = self.get_blocks_dir_or_warn()
        if not blocks_dir:
            return
        cmd = (f'python3 generate_music_prompt.py --blocks-dir "{blocks_dir}" '
               f'--master MASTER_ПРОМТ_MUSIC.txt --output-dir превью')
        self.run_in_background(self.exec_remote, cmd)

    def on_refresh_balance(self):
        self.run_in_background(self._refresh_balance_task)

    def _refresh_balance_task(self):
        cfg = self.config_data
        runpod_text = "—"
        royaltechno_text = "—"

        runpod_key = cfg.get("runpod_api_key")
        if runpod_key:
            try:
                balance = get_account_balance(runpod_key)
                runpod_text = f"${balance:.2f}"
            except Exception as e:
                runpod_text = "ошибка"
                self.log(f"ОШИБКА баланса RunPod: {e}")

        royaltechno_key = cfg.get("royaltechno_api_key")
        if royaltechno_key:
            try:
                balance = get_royaltechno_balance(royaltechno_key)
                if isinstance(balance, (int, float)):
                    royaltechno_text = f"${balance:.2f}"
                elif isinstance(balance, str):
                    royaltechno_text = balance
                else:
                    royaltechno_text = "см. журнал"
                    self.log(f"RoyalTechno вернул непонятный формат баланса: {balance}")
            except Exception as e:
                royaltechno_text = "ошибка"
                self.log(f"ОШИБКА баланса RoyalTechno: {e}")

        self.balance_label.config(text=f"RunPod: {runpod_text}    |    RoyalTechno: {royaltechno_text}")

    def on_pack_output(self):
        cmd = ('cd /workspace/runpod-slim/ComfyUI && '
               'tar -czf output.tar.gz output/ && '
               'ls -la output.tar.gz')
        self.run_in_background(self._pack_output_task, cmd)

    def _pack_output_task(self, cmd):
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return
        self.log("\n>>> Упаковываю папку output в архив на сервере (может занять время)...\n")
        try:
            exit_code = run_command(client, cmd, on_output=self.log)
            self.log(f"\n[завершено, код: {exit_code}]\n")
        except Exception as e:
            self.log(f"ОШИБКА: {e}")

    def on_download_output(self):
        local_dir = filedialog.askdirectory(title="Куда сохранить output.tar.gz")
        if not local_dir:
            return
        self.run_in_background(self._download_output_task, local_dir)

    def _download_output_task(self, local_dir):
        remote_path = "/workspace/runpod-slim/ComfyUI/output.tar.gz"
        local_path = str(Path(local_dir) / "output.tar.gz")

        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        self.log(f"\n>>> Скачиваю {remote_path} -> {local_path}\n")
        self.log("(если архив ещё не создан - сначала нажми '6. Собрать архив на сервере')\n")

        last_logged_percent = [-10]

        def progress(percent, transferred, total):
            if percent - last_logged_percent[0] >= 5:  # логируем не чаще, чем каждые 5%
                last_logged_percent[0] = percent
                mb_done = transferred / 1024 / 1024
                mb_total = total / 1024 / 1024
                self.log(f"  Скачано: {percent}% ({mb_done:.0f} МБ из {mb_total:.0f} МБ)")

        try:
            size = download_file(client, remote_path, local_path, on_progress=progress)
            self.log(f"\nГотово! Скачано {size / 1024 / 1024:.0f} МБ в {local_path}\n")
        except Exception as e:
            self.log(f"ОШИБКА скачивания: {e}")

    def on_download_one_csv(self):
        self.pick_remote_file_async(f"{REMOTE_DIR}/результаты", ".csv", "Выбери CSV для скачивания",
                                     self._on_download_one_csv_picked)

    def _on_download_one_csv_picked(self, csv_name):
        if not csv_name:
            return
        local_dir = filedialog.askdirectory(title="Куда сохранить CSV")
        if not local_dir:
            return
        self.run_in_background(self._download_one_csv_task, csv_name, local_dir)

    def _download_one_csv_task(self, csv_name, local_dir):
        remote_path = f"{REMOTE_DIR}/результаты/{csv_name}"
        local_path = str(Path(local_dir) / csv_name)
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        self.log(f"\n>>> Скачиваю {remote_path} -> {local_path}\n")
        try:
            download_file(client, remote_path, local_path)
            self.log(f"Готово! Сохранено: {local_path}\n")
        except Exception as e:
            self.log(f"ОШИБКА скачивания: {e}")

    def pick_remote_file_async(self, remote_dir: str, extension, title: str, callback):
        """Асинхронно получает список файлов на сервере (в фоне, не блокируя
        окно), затем показывает диалог выбора на главном потоке и вызывает
        callback(имя_файла_или_None)."""
        self.log(f"Читаю список файлов в {remote_dir}...")

        def _fetch():
            try:
                client = self.get_ssh_client()
                files = list_remote_files(client, remote_dir, extension)
            except Exception as e:
                self.log(f"ОШИБКА: {e}")
                self.after(0, lambda: callback(None))
                return
            self.after(0, lambda: self._show_file_picker_dialog(files, remote_dir, title, callback))

        self.run_in_background(_fetch)

    def ask_text_dialog(self, title, prompt, initial_value=""):
        """Своё окошко для ввода текста (вместо tk.simpledialog, который
        иногда открывается ненадёжно) - с тем же принудительным выводом
        поверх всех окон, что и остальные диалоги в программе. Работает
        синхронно (вызывается уже из главного потока), возвращает
        введённый текст или None, если отменили."""
        result = {"value": None}

        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("420x140")
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        ttk.Label(dialog, text=prompt, wraplength=390).pack(fill="x", padx=10, pady=(12, 6))

        var = tk.StringVar(value=initial_value)
        entry = ttk.Entry(dialog, textvariable=var, width=45)
        entry.pack(padx=10, pady=4)
        entry.focus_set()
        entry.select_range(0, "end")
        self._add_context_menu(entry)

        def confirm():
            result["value"] = var.get()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        entry.bind("<Return>", lambda e: confirm())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Button(btn_frame, text="OK", command=confirm).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Отмена", command=cancel).pack(side="right", padx=2)

        dialog.wait_window()
        return result["value"]

    def _show_file_picker_dialog(self, files, remote_dir, title, callback):
        if not files:
            messagebox.showinfo("Пусто", f"В папке {remote_dir} не найдено подходящих файлов.")
            callback(None)
            return

        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("450x400")
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        ttk.Label(dialog, text=remote_dir + "/", wraplength=430).pack(fill="x", padx=10, pady=(10, 4))

        listbox = tk.Listbox(dialog, font=("Consolas", 10))
        listbox.pack(fill="both", expand=True, padx=10, pady=4)
        for f in files:
            listbox.insert("end", f)

        def confirm():
            sel = listbox.curselection()
            name = listbox.get(sel[0]) if sel else None
            dialog.destroy()
            callback(name)

        def cancel():
            dialog.destroy()
            callback(None)

        listbox.bind("<Double-Button-1>", lambda e: confirm())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_frame, text="Выбрать", command=confirm).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Отмена", command=cancel).pack(side="right", padx=2)

    def pick_remote_folder_async(self, callback, start_path=""):
        """Асинхронно получает список папок (в фоне), затем показывает
        окошко выбора (создаётся заново каждый раз - точно так же, как
        уже проверенно работающий выбор файла для кнопки 8), вызывает
        callback(путь_или_None)."""
        full_remote = f"{REMOTE_DIR}/{start_path}".rstrip("/")
        self.log(f"Читаю список папок в {full_remote}...")

        def _fetch():
            try:
                client = self.get_ssh_client()
                dirs = list_remote_dirs(client, full_remote)
            except Exception as e:
                self.log(f"ОШИБКА: {e}")
                self.after(0, lambda: callback(None))
                return
            self.after(0, lambda: self._show_folder_picker_dialog(dirs, start_path, callback))

        self.run_in_background(_fetch)

    def _show_folder_picker_dialog(self, dirs, current_path, callback):
        dialog = tk.Toplevel(self)
        dialog.title("Выбери папку на сервере")
        dialog.geometry("450x400")
        dialog.transient(self)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        ttk.Label(dialog, text=f"automation/{current_path}", wraplength=430).pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(dialog, text="Выдели папку в списке и нажми «Выбрать эту папку», "
                                "или зайди внутрь двойным кликом.",
                  foreground="#666666", wraplength=430).pack(fill="x", padx=10)

        listbox = tk.Listbox(dialog, font=("Consolas", 10))
        listbox.pack(fill="both", expand=True, padx=10, pady=4)
        for d in dirs:
            listbox.insert("end", d)

        def go_into_selected():
            sel = listbox.curselection()
            if not sel:
                return
            name = listbox.get(sel[0])
            new_path = f"{current_path}/{name}".strip("/")
            dialog.destroy()
            self.pick_remote_folder_async(callback, start_path=new_path)

        def go_up():
            new_path = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
            dialog.destroy()
            self.pick_remote_folder_async(callback, start_path=new_path)

        def confirm():
            sel = listbox.curselection()
            if sel:
                # что-то выделено в списке - выбираем именно эту папку,
                # а не ту, внутри которой сейчас находимся
                name = listbox.get(sel[0])
                chosen_path = f"{current_path}/{name}".strip("/")
            else:
                # ничего не выделено - выбираем текущую папку целиком
                chosen_path = current_path
            dialog.destroy()
            callback(chosen_path)

        def cancel():
            dialog.destroy()
            callback(None)

        listbox.bind("<Double-Button-1>", lambda e: go_into_selected())

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(btn_frame, text="⬆ Наверх", command=go_up).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Открыть →", command=go_into_selected).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="Выбрать эту папку", command=confirm).pack(side="right", padx=2)
        ttk.Button(btn_frame, text="Отмена", command=cancel).pack(side="right", padx=2)

    def on_assemble_video(self):
        self.pick_remote_file_async(f"{REMOTE_DIR}/результаты", ".csv", "Выбери CSV блока для сборки видео",
                                     self._on_assemble_video_picked)

    def _on_assemble_video_picked(self, csv_name):
        if not csv_name:
            return
        if csv_name in self.assemble_queue_blocks:
            messagebox.showinfo("Уже в работе", f"{csv_name} уже есть в панели очереди сборки - "
                                                   f"смотри там его статус.")
            return
        self.assemble_queue_blocks.add(csv_name)
        self.assemble_tree.insert("", "end", iid=csv_name, text=csv_name, values=("⏳ В очереди",))
        self.run_in_background(self._run_queued_assemble, csv_name)

    def on_split_audio(self):
        self.pick_remote_file_async(f"{REMOTE_DIR}/результаты", ".csv", "Выбери CSV блока для нарезки аудио",
                                     self._on_split_audio_picked)

    def _on_split_audio_picked(self, csv_name):
        if not csv_name:
            return
        block_name = csv_name[:-4] if csv_name.endswith(".csv") else csv_name  # убираем .csv
        cmd = (f'python3 split_audio_by_csv.py --csv "результаты/{csv_name}" '
               f'--audio "результаты/{block_name}.mp3" '
               f'--output-dir "результаты/{block_name}_audio"')
        self.run_in_background(self.exec_remote, cmd)

    def on_download_folder(self):
        self.pick_remote_folder_async(self._on_download_folder_picked)

    def _on_download_folder_picked(self, subfolder):
        if subfolder is None:
            return
        extension = self.ask_text_dialog(
            "Тип файлов",
            "Расширение файлов для скачивания (например: .mp3, .png, .mp4, "
            "или оставь пустым, чтобы скачать вообще все файлы):",
            initial_value=".mp3")
        if extension is None:
            return
        local_dir = filedialog.askdirectory(title="Куда сохранить папку")
        if not local_dir:
            return
        self.run_in_background(self._download_folder_task, subfolder, extension, local_dir)

    def _download_folder_task(self, subfolder, extension, local_dir):
        remote_dir = f"{REMOTE_DIR}/{subfolder}"
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        # локальная подпапка с тем же названием, чтобы не мешать файлы разных папок
        target_dir = str(Path(local_dir) / Path(subfolder).name)

        self.log(f"\n>>> Скачиваю файлы \"{extension or '(любые)'}\" из {remote_dir} -> {target_dir}\n")
        try:
            files = download_matching_files(client, remote_dir, target_dir, extension, on_output=self.log)
            self.log(f"\nГотово! Скачано файлов: {len(files)} в {target_dir}\n")
        except Exception as e:
            self.log(f"ОШИБКА скачивания: {e}")

    def on_download_all_csv(self):
        local_dir = filedialog.askdirectory(title="Куда сохранить все CSV")
        if not local_dir:
            return
        self.run_in_background(self._download_all_csv_task, local_dir)

    def _download_all_csv_task(self, local_dir):
        remote_dir = f"{REMOTE_DIR}/результаты"
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        self.log(f"\n>>> Скачиваю все .csv из {remote_dir} -> {local_dir}\n")
        try:
            files = download_matching_files(client, remote_dir, local_dir, ".csv", on_output=self.log)
            self.log(f"\nГотово! Скачано файлов: {len(files)}\n")
        except Exception as e:
            self.log(f"ОШИБКА скачивания: {e}")

    def on_split_script(self):
        local_file = filedialog.askopenfilename(
            title="Выбери файл с полным текстом сценария (.txt или .md)",
            filetypes=[("Текстовые файлы", "*.txt *.md"), ("Все файлы", "*.*")])
        if not local_file:
            return

        blocks_dir = self.blocks_dir_var.get().strip()
        if not blocks_dir:
            blocks_dir = "тексты блоков"
            self.blocks_dir_var.set(blocks_dir)

        self.run_in_background(self._split_script_task, local_file, blocks_dir)

    def _split_script_task(self, local_file, blocks_dir):
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        filename = Path(local_file).name
        remote_path = f"{REMOTE_DIR}/{filename}"
        self.log(f"\n>>> Загружаю сценарий {filename} на сервер...\n")
        try:
            upload_file(client, local_file, remote_path)
            self.log("Готово, файл загружен.\n")
        except Exception as e:
            self.log(f"ОШИБКА загрузки: {e}")
            return

        cmd = f'python3 split_script_into_blocks.py --input "{filename}" --output-dir "{blocks_dir}"'
        self.exec_remote(cmd)

    def on_upload_blocks(self):
        local_dir = filedialog.askdirectory(title="Выбери папку с текстами блоков (.txt) на компьютере")
        if not local_dir:
            return

        remote_dir_name = self.blocks_dir_var.get().strip()
        if not remote_dir_name:
            # поле пустое - берём имя самой выбранной папки, чтобы не заливать в корень
            remote_dir_name = Path(local_dir).name
            self.blocks_dir_var.set(remote_dir_name)
            self.log(f"[i] Поле 'Название сценария' было пустым - использую имя папки: {remote_dir_name}")

        self.run_in_background(self._upload_blocks_task, local_dir, remote_dir_name)

    def _upload_blocks_task(self, local_dir, remote_dir_name):
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        remote_dir = f"{REMOTE_DIR}/{remote_dir_name}"
        self.log(f"\n>>> Загружаю файлы из {local_dir} в {remote_dir}...\n")
        try:
            count = upload_directory(client, local_dir, remote_dir, on_output=self.log)
            self.log(f"\nГотово! Загружено файлов: {count}\n")
        except Exception as e:
            self.log(f"ОШИБКА загрузки: {e}")

    def on_upload_file(self):
        local_file = filedialog.askopenfilename(
            title="Выбери файл для загрузки (мастер-промт, библиотека объектов и т.п.)")
        if not local_file:
            return
        self.run_in_background(self._upload_file_task, local_file)

    def _upload_file_task(self, local_file):
        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        filename = Path(local_file).name
        remote_path = f"{REMOTE_DIR}/{filename}"
        self.log(f"\n>>> Загружаю {local_file} -> {remote_path}\n")

        last_logged = [-10]

        def progress(percent, transferred, total):
            if percent - last_logged[0] >= 20:
                last_logged[0] = percent
                self.log(f"  {percent}%...")

        try:
            size = upload_file(client, local_file, remote_path, on_progress=progress)
            self.log(f"\nГотово! Загружено {size / 1024:.0f} КБ -> {remote_path}\n")
        except Exception as e:
            self.log(f"ОШИБКА загрузки: {e}")

    def on_add_to_queue(self):
        raw = self.parallel_csv_var.get().strip()
        if not raw:
            messagebox.showinfo("Не заполнено", "Впиши имена CSV-файлов через запятую")
            return
        csv_files = [c.strip() for c in raw.split(",") if c.strip()]

        added = 0
        for csv_name in csv_files:
            if csv_name in self.queue_blocks:
                continue  # уже в очереди/обрабатывается - не дублируем
            self.queue_blocks.add(csv_name)
            self.queue_tree.insert("", "end", iid=csv_name, text=csv_name, values=("⏳ В очереди",))
            self.run_in_background(self._run_queued_generation, csv_name)
            added += 1

        self.parallel_csv_var.set("")
        if added:
            self.log(f"Добавлено в очередь: {added} блок(ов). "
                      f"Одновременно генерируются максимум 3 - остальные ждут своей очереди.")

    def _set_queue_status(self, csv_name, status):
        def _update():
            if self.queue_tree.exists(csv_name):
                self.queue_tree.item(csv_name, values=(status,))
        self.after(0, _update)

    def _run_queued_generation(self, csv_name):
        # если уже 3 блока генерируются - эта попытка просто ждёт здесь,
        # пока не освободится место (без дополнительного кода для очереди)
        acquired = False
        try:
            waiting_shown = False
            while not self.gen_semaphore.acquire(timeout=3):
                if not waiting_shown:
                    self._set_queue_status(csv_name, "⏳ Ждёт своей очереди (3 уже работают)...")
                    waiting_shown = True
            acquired = True

            self._set_queue_status(csv_name, "🔵 Генерируется...")
            cmd = f'python3 generate_via_api_and_upscale.py --run --csv "результаты/{csv_name}"'
            prefix = f"[{csv_name}] "

            api_key = self.config_data.get("runpod_api_key")
            if self.last_good_ip and self.last_good_port:
                ip, port = self.last_good_ip, self.last_good_port
            else:
                ip, port = get_pod_ssh_connection(api_key)
                self.last_good_ip, self.last_good_port = ip, port

            client = connect(ip, port)
            full_command = self._build_full_command(cmd)
            self.log(f"\n{prefix}>>> {cmd}\n")
            exit_code = run_command(client, full_command, on_output=lambda line: self.log(f"{prefix}{line}"))

            if exit_code == 0:
                self._set_queue_status(csv_name, "✅ Готово")
            else:
                self._set_queue_status(csv_name, f"❌ Ошибка (код {exit_code}) - смотри журнал")
        except Exception as e:
            self.log(f"[{csv_name}] ОШИБКА: {e}")
            self._set_queue_status(csv_name, "❌ Ошибка подключения")
        finally:
            if acquired:
                self.gen_semaphore.release()

    def on_clear_finished_queue(self):
        for csv_name in list(self.queue_blocks):
            if not self.queue_tree.exists(csv_name):
                continue
            status = self.queue_tree.item(csv_name, "values")[0]
            if "✅" in status or "❌" in status:
                self.queue_tree.delete(csv_name)
                self.queue_blocks.discard(csv_name)

    def on_add_to_assemble_queue(self):
        raw = self.assemble_csv_var.get().strip()
        if not raw:
            messagebox.showinfo("Не заполнено", "Впиши имена CSV-файлов через запятую")
            return
        csv_files = [c.strip() for c in raw.split(",") if c.strip()]

        added = 0
        for csv_name in csv_files:
            if csv_name in self.assemble_queue_blocks:
                continue
            self.assemble_queue_blocks.add(csv_name)
            self.assemble_tree.insert("", "end", iid=csv_name, text=csv_name, values=("⏳ В очереди",))
            self.run_in_background(self._run_queued_assemble, csv_name)
            added += 1

        self.assemble_csv_var.set("")
        if added:
            self.log(f"Добавлено в очередь сборки: {added} блок(ов). "
                      f"Собирается только 1 одновременно - остальные ждут своей очереди.")

    def _set_assemble_status(self, csv_name, status):
        def _update():
            if self.assemble_tree.exists(csv_name):
                self.assemble_tree.item(csv_name, values=(status,))
        self.after(0, _update)

    def _run_queued_assemble(self, csv_name):
        acquired = False
        try:
            waiting_shown = False
            while not self.assemble_semaphore.acquire(timeout=3):
                if not waiting_shown:
                    self._set_assemble_status(csv_name, "⏳ Ждёт своей очереди (уже собирается другой блок)...")
                    waiting_shown = True
            acquired = True

            self._set_assemble_status(csv_name, "🔵 Собирается...")
            block_name = csv_name[:-4] if csv_name.endswith(".csv") else csv_name
            cmd = (f'python3 assemble_block_video.py --csv "результаты/{csv_name}" '
                   f'--audio "результаты/{block_name}.mp3" '
                   f'--media-dir "../output" '
                   f'--output "результаты/{block_name}_edit.mp4"')
            prefix = f"[{csv_name}] "

            api_key = self.config_data.get("runpod_api_key")
            if self.last_good_ip and self.last_good_port:
                ip, port = self.last_good_ip, self.last_good_port
            else:
                ip, port = get_pod_ssh_connection(api_key)
                self.last_good_ip, self.last_good_port = ip, port

            client = connect(ip, port)
            full_command = self._build_full_command(cmd)
            self.log(f"\n{prefix}>>> {cmd}\n")
            exit_code = run_command(client, full_command, on_output=lambda line: self.log(f"{prefix}{line}"))

            if exit_code == 0:
                self._set_assemble_status(csv_name, "✅ Готово")
            else:
                self._set_assemble_status(csv_name, f"❌ Ошибка (код {exit_code}) - смотри журнал")
        except Exception as e:
            self.log(f"[{csv_name}] ОШИБКА: {e}")
            self._set_assemble_status(csv_name, "❌ Ошибка подключения")
        finally:
            if acquired:
                self.assemble_semaphore.release()

    def on_clear_finished_assemble_queue(self):
        for csv_name in list(self.assemble_queue_blocks):
            if not self.assemble_tree.exists(csv_name):
                continue
            status = self.assemble_tree.item(csv_name, "values")[0]
            if "✅" in status or "❌" in status:
                self.assemble_tree.delete(csv_name)
                self.assemble_queue_blocks.discard(csv_name)

    # ---------------- генерация через RoyalTechno без RunPod (экономия) ----------------

    def on_generate_images_local(self):
        self.pick_remote_file_async(f"{REMOTE_DIR}/результаты", ".csv",
                                     "Выбери CSV для генерации картинок (шаг A, без RunPod)",
                                     self._on_generate_images_local_picked)

    def _on_generate_images_local_picked(self, csv_name):
        if not csv_name:
            return
        limit_text = self.ask_text_dialog(
            "Сколько сцен обработать?",
            "Для теста введи маленькое число (например 2) - обработаются только первые "
            "N сцен из CSV. Когда всё проверено и работает, сотри число и оставь поле "
            "пустым - тогда обработаются ВСЕ сцены.",
            initial_value="2")
        if limit_text is None:
            return  # нажали "Отмена"
        limit_text = limit_text.strip()
        limit = None
        if limit_text:
            try:
                limit = int(limit_text)
            except ValueError:
                messagebox.showwarning("Некорректное число",
                                        "Нужно ввести целое число (например 2), или оставить поле пустым.")
                return
        self.local_gen_cancel_event.clear()
        self.run_in_background(self._generate_images_local_task, csv_name, limit)

    def _generate_images_local_task(self, csv_name, limit=None):
        block_name = csv_name[:-4] if csv_name.endswith(".csv") else csv_name
        work_dir = LOCAL_GENERATION_DIR / block_name
        raw_dir = work_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        local_csv = work_dir / csv_name
        local_library = work_dir / "OBJECT_LIBRARY.md"

        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        self.log(f"\n>>> Скачиваю {csv_name} и OBJECT_LIBRARY.md для локальной генерации...\n")
        try:
            download_file(client, f"{REMOTE_DIR}/результаты/{csv_name}", str(local_csv))
            download_file(client, f"{REMOTE_DIR}/OBJECT_LIBRARY.md", str(local_library))
        except Exception as e:
            self.log(f"ОШИБКА скачивания CSV/библиотеки: {e}")
            return

        api_key = self.config_data.get("royaltechno_api_key")
        if not api_key:
            self.log("ОШИБКА: не задан RoyalTechno API-ключ (вкладка Настройки)")
            return

        self.log("\n>>> Файлы скачаны - дальше RunPod можно выключить, генерация идёт "
                  "прямо на этом компьютере.\n")
        self.log(f">>> Генерирую картинки (папка: {raw_dir})...\n")
        generate_images(str(local_csv), str(local_library), str(raw_dir), api_key,
                         log=self.log, limit=limit, should_stop=lambda: self.local_gen_cancel_event.is_set())

    def on_upscale_on_runpod(self):
        local_dir = filedialog.askdirectory(
            title="Выбери папку сценария (внутри local_generation) для апскейла (шаг B)",
            initialdir=str(LOCAL_GENERATION_DIR) if LOCAL_GENERATION_DIR.exists() else None)
        if not local_dir:
            return
        self.run_in_background(self._upscale_on_runpod_task, Path(local_dir))

    def _upscale_on_runpod_task(self, work_dir: Path):
        raw_dir = work_dir / "raw"
        upscaled_dir = work_dir / "upscaled"
        upscaled_dir.mkdir(parents=True, exist_ok=True)

        raw_files = [f for f in raw_dir.iterdir() if f.is_file()] if raw_dir.exists() else []
        if not raw_files:
            self.log(f"[!] В {raw_dir} нет сырых картинок - сначала выполни шаг A.")
            return

        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        self.log(f"\n>>> Загружаю {len(raw_files)} сырых картинок на RunPod...\n")
        try:
            upload_directory(client, str(raw_dir), COMFYUI_INPUT_REMOTE_DIR, on_output=self.log)
        except Exception as e:
            self.log(f"ОШИБКА загрузки: {e}")
            return

        self.log("\n>>> Запускаю апскейл на сервере (видеокарта нужна только для этого шага)...\n")
        cmd = (f'python3 upscale_batch.py --input-dir "{COMFYUI_INPUT_REMOTE_DIR}" '
               f'--output-dir "{COMFYUI_OUTPUT_REMOTE_DIR}"')
        self.exec_remote(cmd)

        self.log(f"\n>>> Скачиваю результаты апскейла в {upscaled_dir}...\n")
        try:
            prefixes = {f.stem.replace("_raw", "") for f in raw_files}
            remote_files = list_remote_files(client, COMFYUI_OUTPUT_REMOTE_DIR, ".png")
            matching = [f for f in remote_files if any(f.startswith(p + "_") for p in prefixes)]
            for i, filename in enumerate(matching, 1):
                self.log(f"  [{i}/{len(matching)}] {filename}...")
                download_file(client, f"{COMFYUI_OUTPUT_REMOTE_DIR}/{filename}", str(upscaled_dir / filename))
            self.log(f"\nГотово! Скачано апскейленных картинок: {len(matching)}. "
                      f"Теперь можно выключить RunPod и перейти к шагу C.\n")
        except Exception as e:
            self.log(f"ОШИБКА скачивания результатов: {e}")

    def on_generate_videos_local(self):
        local_dir = filedialog.askdirectory(
            title="Выбери папку сценария (внутри local_generation) для видео (шаг C, без RunPod)",
            initialdir=str(LOCAL_GENERATION_DIR) if LOCAL_GENERATION_DIR.exists() else None)
        if not local_dir:
            return
        self.local_gen_cancel_event.clear()
        self.run_in_background(self._generate_videos_local_task, Path(local_dir))

    def _generate_videos_local_task(self, work_dir: Path):
        upscaled_dir = work_dir / "upscaled"
        video_dir = work_dir / "video"
        csv_matches = list(work_dir.glob("*.csv"))
        if not csv_matches:
            self.log(f"[!] В {work_dir} не найден CSV (он должен был скачаться на шаге A).")
            return
        local_csv = csv_matches[0]

        api_key = self.config_data.get("royaltechno_api_key")
        if not api_key:
            self.log("ОШИБКА: не задан RoyalTechno API-ключ (вкладка Настройки)")
            return

        self.log(f"\n>>> Генерирую видео из апскейленных картинок (папка: {video_dir})...\n")
        generate_videos_from_upscaled(str(local_csv), str(upscaled_dir), str(video_dir), api_key,
                                       log=self.log, should_stop=lambda: self.local_gen_cancel_event.is_set())

    def on_upload_generated_to_runpod(self):
        local_dir = filedialog.askdirectory(
            title="Выбери папку сценария (внутри local_generation) для заливки на RunPod (шаг D)",
            initialdir=str(LOCAL_GENERATION_DIR) if LOCAL_GENERATION_DIR.exists() else None)
        if not local_dir:
            return
        self.run_in_background(self._upload_generated_task, Path(local_dir))

    def _upload_generated_task(self, work_dir: Path):
        upscaled_dir = work_dir / "upscaled"
        video_dir = work_dir / "video"

        try:
            client = self.get_ssh_client()
        except Exception as e:
            self.log(f"ОШИБКА подключения: {e}")
            return

        total = 0
        if upscaled_dir.exists() and any(upscaled_dir.iterdir()):
            self.log(f"\n>>> Заливаю апскейленные картинки из {upscaled_dir}...\n")
            try:
                total += upload_directory(client, str(upscaled_dir), COMFYUI_OUTPUT_REMOTE_DIR, on_output=self.log)
            except Exception as e:
                self.log(f"ОШИБКА загрузки картинок: {e}")
                return

        if video_dir.exists() and any(video_dir.iterdir()):
            self.log(f"\n>>> Заливаю видео из {video_dir}...\n")
            try:
                total += upload_directory(client, str(video_dir), COMFYUI_OUTPUT_REMOTE_DIR, on_output=self.log)
            except Exception as e:
                self.log(f"ОШИБКА загрузки видео: {e}")
                return

        self.log(f"\nГотово! Залито файлов: {total}. Кнопки 11/12 теперь увидят эти "
                  f"картинки/видео как обычно.\n")

    def on_custom_command(self):
        cmd = self.custom_cmd_var.get().strip()
        if not cmd:
            return
        self.run_in_background(self.exec_remote, cmd)

    # ---------------- настройки ----------------

    def on_save_settings(self):
        for key, var in self.settings_vars.items():
            self.config_data[key] = var.get()
        save_config(self.config_data)
        messagebox.showinfo("Готово", "Настройки сохранены.")


if __name__ == "__main__":
    app = App()
    app.mainloop()
