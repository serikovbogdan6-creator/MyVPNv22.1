from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import winreg
from pathlib import Path
from typing import Optional

os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
if os.environ.get("INTOURIST_FORCE_SOFTWARE_RENDER") == "1":
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--disable-gpu --disable-software-rasterizer --disable-gpu-compositing",
    )

import requests

try:
    import psutil
except ImportError:
    psutil = None

from PyQt6.QtCore import QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QPalette, QColor, QIcon
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel

from vpn_bridge_api import VPNBridgeAPI


def _install_crash_safety_net():
    """
    ВАЖНО: PyQt6 по умолчанию завершает ВЕСЬ процесс, если необработанное
    исключение вылетает внутри слота (обработчика сигнала) — это выглядит
    как внезапный краш всего приложения, хотя реальная причина — обычная
    Python-ошибка в одном методе. Перехватываем её сами: пишем в лог и НЕ
    даём процессу падать.
    """
    import traceback

    def handle_exception(exc_type, exc_value, exc_tb):
        try:
            log_path = Path(os.environ.get("TEMP", ".")) / "intourist_vpn_gui_crash.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} (unhandled) ---\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                f.write("\n")
        except Exception:
            pass
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = handle_exception


def _ensure_admin():
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False

    if not is_admin:
        params = " ".join(f'"{a}"' for a in sys.argv)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            if result > 32:
                sys.exit(0)
        except Exception:
            pass


def _resolve_base() -> Path:
    """Найти базовую папку (где мы ищем ресурсы)."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir
    return Path(__file__).resolve().parent


def _find_ui_file() -> Optional[Path]:
    """
    Ищет intourist_vps_premium_ui/index.html в нескольких местах.
    Возвращает полный путь к файлу или None.
    """
    base = _resolve_base()
    candidates = [
        base / "intourist_vps_premium_ui" / "index.html",
        base / "_internal" / "intourist_vps_premium_ui" / "index.html",
        base.parent / "intourist_vps_premium_ui" / "index.html",
    ]
    
    for path in candidates:
        if path.exists():
            return path
    return None


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ────────────────────────── сохранение состояния ────────────────────────

MAX_LINK_HISTORY = 10


def _state_file_path() -> Path:
    """Файл, в котором сохраняются состояние приложения между запусками."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    d = root / "IntouristVPN"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = _exe_dir()
    return d / "app_state.json"


def _load_app_state() -> dict:
    """Загружает состояние приложения из файла."""
    try:
        path = _state_file_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {
        "link_history": [],
        "servers": [],
        "last_selected_host": "",
    }


def _save_app_state(state: dict):
    """Сохраняет состояние приложения в файл."""
    try:
        path = _state_file_path()
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _link_history_path() -> Path:
    """Файл, в котором между запусками программы хранятся ранее введённые ссылки."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    d = root / "IntouristVPN"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = _exe_dir()
    return d / "link_history.json"


def _load_link_history() -> list[str]:
    try:
        path = _link_history_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()][:MAX_LINK_HISTORY]
    except Exception:
        pass
    return []


def _save_link_history(links: list[str]):
    try:
        path = _link_history_path()
        path.write_text(
            json.dumps(links[:MAX_LINK_HISTORY], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ────────────────────────── форматирование трафика ───────────────────────

def _format_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)} Б"
    n /= 1024
    for unit in ("КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


COUNTRY_FLAGS = {
    "ru": "🇷🇺", "us": "🇺🇸", "de": "🇩🇪", "nl": "🇳🇱", "fi": "🇫🇮",
    "fr": "🇫🇷", "gb": "🇬🇧", "jp": "🇯🇵", "sg": "🇸🇬", "ua": "🇺🇦",
    "pl": "🇵🇱", "se": "🇸🇪", "tr": "🇹🇷", "ir": "🇮🇷", "bg": "🇧🇬",
    "es": "🇪🇸",
}


def _flag(host: str) -> str:
    low = (host or "").lower()
    for cc, flag in COUNTRY_FLAGS.items():
        if cc in low:
            return flag
    return "🌍"


HELPER_SERVER = {
    "name": "Обход белых списков", "location": "Локальный режим",
    "host": "local", "port": 0, "protocol": "Helper", "transport": "direct",
    "cred": "", "params": {}, "kind": "helper", "ping": None, "country": None,
}

_COUNTRY_NAME_HINTS = {
    "ru": ("russia", "россия", "москва", "moscow", "рф"),
    "nl": ("netherlands", "нидерланды", "amsterdam", "амстердам"),
    "bg": ("bulgaria", "болгария", "sofia", "софия"),
    "de": ("germany", "германия", "frankfurt", "франкфурт", "berlin", "берлин"),
    "pl": ("poland", "польша", "warsaw", "варшава"),
    "jp": ("japan", "япония", "tokyo", "токио"),
    "us": ("usa", "united states", "сша", "america", "нью-йорк", "new york"),
    "gb": ("uk", "united kingdom", "англия", "london", "лондон", "великобритания"),
    "fr": ("france", "франция", "paris", "париж"),
    "es": ("spain", "испания", "madrid", "мадрид"),
    "fi": ("finland", "финляндия", "helsinki", "хельсинки"),
    "se": ("sweden", "швеция", "stockholm", "стокгольм"),
    "tr": ("turkey", "турция", "istanbul", "стамбул"),
    "ua": ("ukraine", "украина", "kiev", "киев", "kyiv"),
    "sg": ("singapore", "сингапур"),
}


def _guess_country(name: str, host: str) -> Optional[str]:
    """Пытается угадать код страны по названию/хосту сервера.
    Использует fallback для гарантии результата."""
    text = f"{name} {host}".lower()
    
    # эмодзи-флаг (пара regional indicator symbols) прямо в названии
    for ch1, ch2 in zip(text, text[1:]):
        if "\U0001F1E6" <= ch1 <= "\U0001F1FF" and "\U0001F1E6" <= ch2 <= "\U0001F1FF":
            code = chr(ord(ch1) - 0x1F1E6 + ord("a")) + chr(ord(ch2) - 0x1F1E6 + ord("a"))
            if code in _COUNTRY_NAME_HINTS:
                return code
    
    # Поиск по подстроке с высокой приоритетностью для точных совпадений
    for code, hints in _COUNTRY_NAME_HINTS.items():
        # Сначала ищем точное совпадение слова
        for hint in hints:
            if f" {hint} " in f" {text} " or text.startswith(hint) or text.endswith(hint):
                return code
    
    # Затем обычный поиск подстроки
    for code, hints in _COUNTRY_NAME_HINTS.items():
        if any(h in text for h in hints):
            return code
        if f".{code}" in host.lower() or host.lower().endswith(code):
            return code
    
    # Fallback: пытаемся извлечь код из TLD хоста
    if "." in host:
        parts = host.split(".")
        potential_code = parts[-1].lower()
        if potential_code in _COUNTRY_NAME_HINTS:
            return potential_code
    
    return None  # Fallback: будет использоваться DEFAULT_FLAG в UI


# ────────────────────────── парсинг подписок ────────────────────────────

def parse_uri(uri: str) -> Optional[dict]:
    uri = uri.strip()
    try:
        if uri.startswith("vless://"):
            p = urllib.parse.urlparse(uri)
            qs = dict(urllib.parse.parse_qsl(p.query))
            name = urllib.parse.unquote(p.fragment) or p.hostname or "VLESS"
            return {
                "name": name, "host": p.hostname, "port": p.port or 443,
                "protocol": "VLESS", "transport": qs.get("type", "tcp"),
                "cred": p.username, "params": qs,
                "location": p.hostname or "", "kind": "sub", "ping": None,
                "country": _guess_country(name, p.hostname or ""),
            }
        if uri.startswith("trojan://"):
            p = urllib.parse.urlparse(uri)
            qs = dict(urllib.parse.parse_qsl(p.query))
            name = urllib.parse.unquote(p.fragment) or p.hostname or "Trojan"
            return {
                "name": name, "host": p.hostname, "port": p.port or 443,
                "protocol": "Trojan", "transport": qs.get("type", "tcp"),
                "cred": p.username, "params": qs,
                "location": p.hostname or "", "kind": "sub", "ping": None,
                "country": _guess_country(name, p.hostname or ""),
            }
        if uri.startswith("ss://"):
            p = urllib.parse.urlparse(uri)
            name = urllib.parse.unquote(p.fragment) or p.hostname or "SS"
            try:
                userinfo = base64.b64decode(p.username + "==").decode()
                method, password = userinfo.split(":", 1)
            except Exception:
                method, password = "aes-256-gcm", p.username or ""
            return {
                "name": name, "host": p.hostname, "port": p.port or 443,
                "protocol": "Shadowsocks", "transport": method,
                "cred": password, "params": {},
                "location": p.hostname or "", "kind": "sub", "ping": None,
                "country": _guess_country(name, p.hostname or ""),
            }
        if uri.startswith("vmess://"):
            data = base64.b64decode(uri[8:] + "==").decode()
            d = json.loads(data)
            name = d.get("ps") or d.get("add") or "VMess"
            return {
                "name": name, "host": d.get("add", ""), "port": int(d.get("port", 443)),
                "protocol": "VMess", "transport": d.get("net", "tcp"),
                "cred": d.get("id", ""), "params": d,
                "location": d.get("add", ""), "kind": "sub", "ping": None,
                "country": _guess_country(name, d.get("add", "")),
            }
    except Exception:
        pass
    return None


def parse_subscription(raw: str) -> list[dict]:
    servers: list[dict] = []
    try:
        decoded = base64.b64decode(raw.strip() + "==").decode(errors="ignore")
        raw = decoded
    except Exception:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        srv = parse_uri(line)
        if srv:
            servers.append(srv)
    return servers


# ────────────────────────── воркеры ─────────────────────────────────────

class SubWorker(QThread):
    done = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            r = requests.get(self.url, timeout=15)
            r.raise_for_status()
            servers = parse_subscription(r.text)
            self.done.emit(servers)
        except Exception as exc:
            self.error.emit(str(exc))


class PingWorker(QThread):
    result = pyqtSignal(int, int)

    CURL_TIMEOUT_SEC = 6

    def __init__(self, index: int, host: str, port: int):
        super().__init__()
        self.index, self.host, self.port = index, host, port

    def run(self):
        ms = self._ping_via_curl()
        if ms is None:
            ms = self._ping_via_socket()
        self.result.emit(self.index, ms if ms is not None else -1)

    def _ping_via_curl(self) -> Optional[int]:
        try:
            devnull = "NUL" if os.name == "nt" else "/dev/null"
            cmd = [
                "curl", "-s", "-k", "-o", devnull,
                "--connect-timeout", "5",
                "-m", str(self.CURL_TIMEOUT_SEC),
                "-w", "%{time_connect}",
                f"https://{self.host}:{self.port}/",
            ]
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.CURL_TIMEOUT_SEC + 2, **kwargs,
            )
            value = (out.stdout or "").strip()
            if value:
                seconds = float(value)
                if seconds > 0:
                    return int(seconds * 1000)
        except Exception:
            pass
        return None

    def _ping_via_socket(self) -> Optional[int]:
        try:
            t0 = time.monotonic()
            with socket.create_connection((self.host, self.port), timeout=5):
                pass
            return int((time.monotonic() - t0) * 1000)
        except Exception:
            return None


class VpnWorker(QThread):
    log = pyqtSignal(str)
    started_ok = pyqtSignal()
    stopped = pyqtSignal()
    start_failed = pyqtSignal(str)

    READY_MARKERS = (
        "myvpn connected", "socks5 ready", "upstream connected",
        "tun2socks: started", "xray", "started",
    )
    READY_TIMEOUT_SEC = 3.0

    def __init__(self, cmd: list[str]):
        super().__init__()
        self.cmd = cmd
        self._proc: Optional[subprocess.Popen] = None
        self._ready_emitted = False
        self._ready_lock = threading.Lock()

    def _mark_ready(self):
        with self._ready_lock:
            if self._ready_emitted:
                return
            self._ready_emitted = True
        self.started_ok.emit()

    def _ready_timeout_check(self):
        if self._proc and self._proc.poll() is None:
            self._mark_ready()

    def run(self):
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.log.emit(f"[ERROR] Не удалось запустить процесс: {exc}")
            self.start_failed.emit(str(exc))
            self.stopped.emit()
            return

        timer = threading.Timer(self.READY_TIMEOUT_SEC, self._ready_timeout_check)
        timer.daemon = True
        timer.start()

        exit_code = None
        try:
            for line in self._proc.stdout:
                line = line.rstrip()
                if line:
                    self.log.emit(line)
                if not self._ready_emitted:
                    low = line.lower()
                    if any(m in low for m in self.READY_MARKERS):
                        self._mark_ready()
            exit_code = self._proc.wait()
        except Exception as exc:
            self.log.emit(f"[ERROR] {exc}")
        finally:
            timer.cancel()
            if not self._ready_emitted and exit_code not in (None, 0):
                self.start_failed.emit(f"Процесс завершился с кодом {exit_code} до готовности.")
            self.stopped.emit()

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                )
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass


# ────────────────────────── главное окно ────────────────────────────────

class MainWindow(QMainWindow):
    BASE = _resolve_base()
    EXE_DIR = _exe_dir()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Intourist VPN  v2.2")
        self.setMinimumSize(900, 620)
        self.resize(1040, 780)

        self._vpn_worker: Optional[VpnWorker] = None
        self._sub_worker: Optional[SubWorker] = None
        self._ping_workers: list[PingWorker] = []
        self._connected = False
        self._conn_mode: Optional[str] = None
        self._original_dns: dict[str, dict] = {}
        self._dns_adapters: list[str] = []
        self._servers: list[dict] = [HELPER_SERVER]
        self._connection_time = 0
        self._net_baseline = None
        self._link_history: list[str] = _load_link_history()
        self._pending_sub_link: Optional[str] = None
        self._disconnect_in_progress = False  # Флаг для синхронизации отключения
        self._state_lock = threading.Lock()  # Mutex для защиты состояния

        # Загружаем сохраненное состояние
        self._load_saved_state()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._web_view = QWebEngineView()
        settings = self._web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

        self._channel = QWebChannel()
        self._bridge = VPNBridgeAPI()
        self._bridge.connectionRequested.connect(self._on_connect_requested)
        self._bridge.disconnectionRequested.connect(self._disconnect)
        self._bridge.subscriptionRequested.connect(self._load_subscription)
        self._bridge.pingRequested.connect(self._on_manual_ping)
        self._channel.registerObject("bridge", self._bridge)
        self._web_view.page().setWebChannel(self._channel)

        ui_path = _find_ui_file()
        if ui_path:
            self._web_view.setUrl(QUrl.fromLocalFile(str(ui_path)))
        else:
            base = _resolve_base()
            error_msg = f"""
            <h1 style='color:#f44;background:#111;padding:20px'>UI файл не найден</h1>
            <pre style='color:#aaa;background:#000;padding:20px;font-family:monospace;overflow:auto'>
Ищем в:
  • {base / "intourist_vps_premium_ui" / "index.html"}
  • {base / "_internal" / "intourist_vps_premium_ui" / "index.html"}
  • {base.parent / "intourist_vps_premium_ui" / "index.html"}

Рабочая директория: {Path.cwd()}
Exe: {Path(sys.executable)}

Копируй папку intourist_vps_premium_ui рядом с exe и перезапусти приложение.
            </pre>
            """
            self._web_view.setHtml(error_msg)
        self._web_view.loadFinished.connect(self._on_page_loaded)

        root.addWidget(self._web_view)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._on_auto_ping)
        self._ping_timer.start(30_000)

    def _load_saved_state(self):
        """Загружает сохраненное состояние приложения."""
        try:
            state = _load_app_state()
            if state.get("servers"):
                self._servers = [HELPER_SERVER] + state["servers"]
                if state.get("last_selected_host"):
                    self._bridge._last_selected_host = state["last_selected_host"]
        except Exception:
            pass

    def _save_current_state(self):
        """Сохраняет текущее состояние приложения."""
        try:
            with self._state_lock:
                # Сохраняем только серверы из подписок (без Helper)
                subscription_servers = [s for s in self._servers if s.get("kind") != "helper"]
                state = {
                    "link_history": self._link_history,
                    "servers": subscription_servers,
                    "last_selected_host": self._bridge._last_selected_host,
                }
                _save_app_state(state)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _on_page_loaded(self, ok: bool):
        """Публикуем начальный список серверов после загрузки страницы."""
        self._bridge.setServers(self._servers)
        self._bridge.setStatus(False)
        self._bridge.setLinkHistory(self._link_history)

    # ------------------------------------------------------------------
    # Подписки
    # ------------------------------------------------------------------
    def _load_subscription(self, text: str):
        text = text.strip()
        if not text:
            return

        if any(text.startswith(p) for p in ("vless://", "vmess://", "ss://", "trojan://")):
            srv = parse_uri(text)
            if srv:
                self._servers = [HELPER_SERVER, srv]
                self._bridge.setServers(self._servers)
                self._bridge.appendLog("[INFO] Сервер добавлен из URI.")
                self._remember_link(text)
                self._save_current_state()
                self._start_pings()
            else:
                self._bridge.appendLog("[ERROR] Не удалось разобрать URI.")
            return

        self._bridge.appendLog("[INFO] Загрузка подписки...")
        self._pending_sub_link = text
        self._sub_worker = SubWorker(text)
        self._sub_worker.done.connect(self._on_sub_done)
        self._sub_worker.error.connect(self._on_sub_error)
        self._sub_worker.start()

    def _on_sub_done(self, servers: list[dict]):
        if not servers:
            self._bridge.appendLog("[WARN] Подписка пуста или не распознана.")
            return
        self._servers = [HELPER_SERVER] + servers
        self._bridge.setServers(self._servers)
        self._bridge.appendLog(f"[INFO] Загружено серверов: {len(servers)}")
        if getattr(self, "_pending_sub_link", None):
            self._remember_link(self._pending_sub_link)
            self._pending_sub_link = None
        self._save_current_state()
        self._start_pings()

    def _on_sub_error(self, msg: str):
        self._bridge.appendLog(f"[ERROR] Ошибка загрузки подписки: {msg}")

    def _remember_link(self, link: str):
        """Сохраняет успешно использованную ссылку в памяти программы."""
        link = (link or "").strip()
        if not link:
            return
        history = [l for l in self._link_history if l != link]
        history.insert(0, link)
        self._link_history = history[:MAX_LINK_HISTORY]
        _save_link_history(self._link_history)
        self._bridge.setLinkHistory(self._link_history)

    def _start_pings(self, *, announce: bool = False):
        pingable = [s for s in self._servers if s.get("kind") != "helper"]
        if announce:
            if pingable:
                self._bridge.appendLog(f"[INFO] Проверка пинга ({len(pingable)} серверов) через curl...")
            else:
                self._bridge.appendLog("[INFO] Нет серверов для проверки пинга.")

        for w in self._ping_workers:
            w.quit()
        self._ping_workers = []
        for i, srv in enumerate(self._servers):
            if srv.get("kind") == "helper":
                continue
            w = PingWorker(i, srv["host"], srv["port"])
            w.result.connect(self._on_ping)
            w.start()
            self._ping_workers.append(w)

    def _on_ping(self, index: int, ms: int):
        with self._state_lock:
            if 0 <= index < len(self._servers):
                self._servers[index]["ping"] = ms
                self._bridge.setServers(self._servers)

    def _on_manual_ping(self):
        """Кнопка 'Пинг' в интерфейсе — проверка по требованию пользователя."""
        self._start_pings(announce=True)

    def _on_auto_ping(self):
        """Срабатывает каждые 30 секунд — автоматическая фоновая проверка."""
        if len(self._servers) > 1:
            self._start_pings(announce=False)

    # ------------------------------------------------------------------
    # Подключение / отключение
    # ------------------------------------------------------------------
    def _on_connect_requested(self, server: dict):
        try:
            # Если уже подключено/подключается — сначала отключаемся
            if self._connected or (self._vpn_worker and self._vpn_worker.isRunning()):
                self._bridge.appendLog("[INFO] Переключение на другой сервер...")
                self._disconnect()
                # Ждем полной очистки перед переподключением
                timeout = 0
                while (self._vpn_worker and self._vpn_worker.isRunning() or 
                       self._disconnect_in_progress) and timeout < 50:
                    time.sleep(0.1)
                    timeout += 1

            if server.get("kind") == "helper":
                self._do_connect_helper()
            else:
                self._connect_via_sub(server)
        except Exception as exc:
            self._bridge.appendLog(f"[ERROR] Подключение не удалось: {exc}")
            self._log_crash()

    def _do_connect_helper(self):
        try:
            myvpn = self.EXE_DIR / "myvpn.exe"
            if not myvpn.exists():
                myvpn = self.BASE / "myvpn.exe"
            if not myvpn.exists():
                self._bridge.appendLog(f"[ERROR] myvpn.exe не найден: {myvpn}")
                return
            self._bridge.setMode("Режим: Обход белых списков (helper)")
            self._conn_mode = "helper"
            self._connection_time = 0
            self._timer.start(1000)
            self._launch_vpn([str(myvpn), "--base-dir", str(self.BASE)])
        except Exception as exc:
            self._bridge.appendLog(f"[ERROR] helper: {exc}")
            self._log_crash()

    def _connect_via_sub(self, srv: dict):
        try:
            from config_gen import make_xray_config, write_config
            cfg = make_xray_config(srv)
            cfg_path = self.EXE_DIR / "config.json"
            write_config(cfg, cfg_path)
            self._bridge.appendLog(f"[INFO] Конфиг записан: {cfg_path}")
        except ImportError:
            self._bridge.appendLog("[WARN] config_gen не найден — fallback на helper.")
            self._do_connect_helper()
            return
        except Exception as exc:
            self._bridge.appendLog(f"[ERROR] config_gen: {exc}")
            self._log_crash()
            return

        try:
            xray = self.EXE_DIR / "bin" / "xray.exe"
            if not xray.exists():
                xray = self.EXE_DIR / "xray.exe"
            if not xray.exists():
                self._bridge.appendLog("[WARN] xray.exe не найден, fallback на helper.")
                self._do_connect_helper()
                return

            server_name = srv.get('name', 'Unknown')
            protocol = srv.get('protocol', 'Unknown')
            self._bridge.setMode(f"Режим: {protocol} / {server_name}")
            self._conn_mode = "sub"
            self._connection_time = 0
            self._timer.start(1000)
            self._launch_vpn([str(xray), "run", "-c", str(cfg_path)])
        except Exception as exc:
            self._bridge.appendLog(f"[ERROR] xray: {exc}")
            self._log_crash()

    def _launch_vpn(self, cmd: list[str]):
        self._reset_traffic_counters()
        self._vpn_worker = VpnWorker(cmd)
        self._vpn_worker.log.connect(self._bridge.appendLog)
        self._vpn_worker.started_ok.connect(self._on_vpn_ready)
        self._vpn_worker.stopped.connect(self._on_vpn_stopped)
        self._vpn_worker.start_failed.connect(self._on_vpn_start_failed)
        self._vpn_worker.start()
        self._bridge.appendLog(f"[INFO] Запуск: {' '.join(cmd)}")

    def _on_vpn_start_failed(self, msg: str):
        self._bridge.appendLog(f"[ERROR] Не удалось подключиться: {msg}")
        self._bridge.appendLog("[HINT] Проверьте корректность ключа/подписки и доступность сервера.")

    def _on_vpn_ready(self):
        try:
            self._update_status(True)
            if self._conn_mode == "sub":
                self._set_proxy(True)
                self._bridge.appendLog("[INFO] Intourist VPN готов. Прокси включён.")
            else:
                self._set_proxy(False)
                self._bridge.appendLog("[INFO] Intourist VPN готов (helper, полный туннель).")
            self._set_dns(True)
            self._save_current_state()
        except Exception as exc:
            self._bridge.appendLog(f"[ERROR] _on_vpn_ready: {exc}")
            self._log_crash()

    def _disconnect(self):
        """Синхронная очистка всего состояния VPN."""
        try:
            with self._state_lock:
                self._disconnect_in_progress = True
            
            if self._vpn_worker and self._vpn_worker.isRunning():
                self._bridge.appendLog("[INFO] Остановка VPN-процесса...")
                self._vpn_worker.stop()
                if not self._vpn_worker.wait(5000):
                    self._bridge.appendLog("[WARN] VPN-процесс не завершился в срок, принудительное завершение.")
        except Exception as exc:
            self._bridge.appendLog(f"[WARN] Ошибка при остановке процесса: {exc}")
        finally:
            # Полностью очищаем состояние
            try:
                self._set_proxy(False)
            except Exception:
                pass
            try:
                self._set_dns(False)
            except Exception:
                pass
            with self._state_lock:
                self._conn_mode = None
                self._disconnect_in_progress = False
            self._update_status(False)
            self._bridge.setMode("")
            self._bridge.appendLog("[INFO] Отключено.")
            self._timer.stop()
            self._save_current_state()

    def _on_vpn_stopped(self):
        try:
            self._update_status(False)
            self._set_proxy(False)
            self._set_dns(False)
        except Exception as exc:
            self._bridge.appendLog(f"[WARN] _on_vpn_stopped: {exc}")
        finally:
            with self._state_lock:
                self._conn_mode = None
            self._bridge.appendLog("[INFO] VPN-процесс завершён.")
            self._timer.stop()
            self._save_current_state()

    def _update_status(self, connected: bool):
        self._connected = connected
        self._bridge.setStatus(connected)

    def _reset_traffic_counters(self):
        """Запоминает текущее значение системных счётчиков трафика."""
        self._net_baseline = None
        if psutil is not None:
            try:
                self._net_baseline = psutil.net_io_counters()
            except Exception:
                self._net_baseline = None

    def _current_traffic(self) -> tuple[str, str]:
        if psutil is None or self._net_baseline is None:
            return "н/д", "н/д"
        try:
            cur = psutil.net_io_counters()
            down = max(0, cur.bytes_recv - self._net_baseline.bytes_recv)
            up = max(0, cur.bytes_sent - self._net_baseline.bytes_sent)
            return _format_bytes(down), _format_bytes(up)
        except Exception:
            return "н/д", "н/д"

    def _tick(self):
        self._connection_time += 1
        h = self._connection_time // 3600
        m = (self._connection_time % 3600) // 60
        s = self._connection_time % 60
        downloaded, uploaded = self._current_traffic()
        self._bridge.setMetrics({
            "time": f"{h:02d}:{m:02d}:{s:02d}",
            "downloaded": downloaded, "uploaded": uploaded, "dns": "1.1.1.1",
        })

    # ------------------------------------------------------------------
    # Прокси / DNS
    # ------------------------------------------------------------------
    @staticmethod
    def _set_proxy(enable: bool):
        REG_INET = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        SOCKS = "socks=127.0.0.1:1080"
        BYPASS = "localhost;127.*;10.*;172.16.*;192.168.*;<local>"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_INET,
                                 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, SOCKS)
                    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, BYPASS)
                else:
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        except Exception:
            pass
        try:
            wininet = ctypes.windll.wininet
            wininet.InternetSetOptionW(None, 39, None, 0)
            wininet.InternetSetOptionW(None, 37, None, 0)
        except Exception:
            pass
        try:
            if enable:
                subprocess.run(["netsh", "winhttp", "set", "proxy", "127.0.0.1:1080", BYPASS],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.run(["netsh", "winhttp", "reset", "proxy"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
        MainWindow._broadcast_proxy_change()

    @staticmethod
    def _broadcast_proxy_change():
        try:
            HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
            result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Internet Settings",
                SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
            )
        except Exception:
            pass

    def _find_internet_adapter(self) -> Optional[str]:
        try:
            ps_script = (
                "Get-WmiObject Win32_NetworkAdapterConfiguration "
                "| Where-Object { $_.IPEnabled -and $_.DefaultIPGateway } "
                "| ForEach-Object { $desc = $_.Description; "
                "$adapter = Get-WmiObject Win32_NetworkAdapter | Where-Object { $_.Description -eq $desc }; "
                "if ($adapter) { $adapter.NetConnectionID } } | Select-Object -First 1"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            name = result.stdout.strip()
            if name:
                return name
        except Exception:
            pass
        try:
            route = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True, text=True,
                                    timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            iface_ip = None
            for line in route.stdout.split("\n"):
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    iface_ip = parts[3]
                    break
            if iface_ip:
                addrs = subprocess.run(["netsh", "interface", "ipv4", "show", "addresses"],
                                        capture_output=True, text=True, timeout=5,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                current_iface = None
                for line in addrs.stdout.split("\n"):
                    if "Configuration for interface" in line:
                        current_iface = line.split('"')[1] if '"' in line else None
                    elif iface_ip in line and current_iface:
                        return current_iface
        except Exception:
            pass
        return None

    def _get_dns_info(self, adapter_name: str) -> dict:
        info = {"dhcp": False, "servers": []}
        try:
            result = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "dnsservers", f"name={adapter_name}"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out = result.stdout
            if "dhcp" in out.lower():
                info["dhcp"] = True
            for line in out.split("\n"):
                parts = line.strip().split()
                if parts and self._is_ip_address(parts[-1]):
                    info["servers"].append(parts[-1])
        except Exception:
            pass
        return info

    def _set_dns(self, enable: bool):
        try:
            if enable:
                adapter = self._find_internet_adapter()
                if not adapter:
                    return
                if adapter not in self._original_dns:
                    self._original_dns[adapter] = self._get_dns_info(adapter)
                    self._dns_adapters.append(adapter)
                subprocess.run(["netsh", "interface", "ipv4", "set", "dnsservers",
                                 f"name={adapter}", "source=static", "address=1.1.1.1", "validate=no"],
                                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                subprocess.run(["netsh", "interface", "ipv4", "add", "dnsservers",
                                 f"name={adapter}", "address=8.8.8.8", "validate=no"],
                                capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                for adapter in list(self._dns_adapters):
                    info = self._original_dns.get(adapter, {})
                    try:
                        if not info or info.get("dhcp"):
                            subprocess.run(["netsh", "interface", "ipv4", "set", "dnsservers",
                                             f"name={adapter}", "source=dhcp"],
                                            capture_output=True, timeout=5,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            servers = info.get("servers", [])
                            if servers:
                                subprocess.run(["netsh", "interface", "ipv4", "set", "dnsservers",
                                                 f"name={adapter}", "source=static",
                                                 f"address={servers[0]}", "validate=no"],
                                                capture_output=True, timeout=5,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                                for dns in servers[1:]:
                                    subprocess.run(["netsh", "interface", "ipv4", "add", "dnsservers",
                                                     f"name={adapter}", f"address={dns}", "validate=no"],
                                                    capture_output=True, timeout=5,
                                                    creationflags=subprocess.CREATE_NO_WINDOW)
                    except Exception:
                        pass
                self._dns_adapters.clear()
                self._original_dns.clear()
        except Exception:
            pass

    @staticmethod
    def _is_ip_address(s: str) -> bool:
        try:
            parts = s.split(".")
            return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
        except (ValueError, AttributeError):
            return False

    def closeEvent(self, event):
        try:
            self._disconnect()
        except Exception:
            self._log_crash()
        finally:
            for w in self._ping_workers:
                try:
                    w.quit()
                except Exception:
                    pass
            self._timer.stop()
            self._ping_timer.stop()
            self._save_current_state()
            event.accept()

    @staticmethod
    def _log_crash():
        import traceback
        try:
            log_path = Path(os.environ.get("TEMP", ".")) / "intourist_vpn_gui_crash.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass


def main():
    _install_crash_safety_net()
    _ensure_admin()

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Intourist VPN")

    icon_path = _resolve_base() / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#06090d"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f3f7fb"))
    app.setPalette(palette)

    win = MainWindow()
    app.aboutToQuit.connect(lambda: win._disconnect())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
