"""
vpn_bridge_api.py — WebChannel-мост между веб-интерфейсом (intourist_vps_premium_ui)
и Python-бэкендом (myvpn_gui.py).

Все данные между Python и JS передаются в виде JSON-строк — это исключает
проблемы с автоконвертацией типов QVariant/PyQt6 и работает предсказуемо
в любой версии PyQt6-WebEngine.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices


class VPNBridgeAPI(QObject):
    """Экземпляр регистрируется в QWebChannel под именем 'bridge'."""

    # ---- Python -> JS (сигналы, на которые подписывается JS) ----------
    logMessage      = pyqtSignal(str)         # одна строка лога
    statusChanged   = pyqtSignal(bool)        # True = подключено
    metricsUpdated  = pyqtSignal(str)         # JSON: {time, downloaded, uploaded, dns}
    serversUpdated  = pyqtSignal(str)         # JSON: [ {name, host, port, protocol, ...}, ... ]
    modeChanged     = pyqtSignal(str)         # текст текущего режима подключения
    linkHistoryUpdated = pyqtSignal(str)      # JSON: [ "ссылка1", "ссылка2", ... ]

    # ---- JS -> Python (слоты, которые вызывает JS) ---------------------
    connectionRequested     = pyqtSignal(dict)   # выбранный сервер (dict)
    disconnectionRequested  = pyqtSignal()
    subscriptionRequested   = pyqtSignal(str)    # URL подписки / одиночный URI
    pingRequested            = pyqtSignal()      # ручной запрос перепроверки пинга

    def __init__(self):
        super().__init__()
        self.servers: list[dict] = []
        self.selected_index: int = -1
        self._last_selected_host: str = ""  # Сохраняем хост выбранного сервера

    # ------------------------------------------------------------------
    # Вызывается из JavaScript
    # ------------------------------------------------------------------
    @pyqtSlot(int)
    def selectServer(self, index: int):
        if 0 <= index < len(self.servers):
            self.selected_index = index
            self._last_selected_host = self.servers[index].get("host", "")

    @pyqtSlot()
    def connectSelected(self):
        if 0 <= self.selected_index < len(self.servers):
            self.connectionRequested.emit(self.servers[self.selected_index])
        elif self.servers:
            # ничего не выбрано — подключаемся к первому (обычно это helper)
            self.connectionRequested.emit(self.servers[0])

    @pyqtSlot()
    def disconnectVpn(self):
        self.disconnectionRequested.emit()

    @pyqtSlot(str)
    def loadSubscription(self, url: str):
        url = (url or "").strip()
        if url:
            self.subscriptionRequested.emit(url)

    @pyqtSlot()
    def pingServers(self):
        """Вызывается из JS кнопкой 'Пинг' — ручной перезапуск проверки пинга."""
        self.pingRequested.emit()

    @pyqtSlot(str)
    def openUrl(self, url: str):
        """Открыть ссылку в системном браузере (внутри QWebEngineView
        обычный target="_blank" ненадёжен без реализации createWindow)."""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    # Вызывается из Python (обновление UI)
    # ------------------------------------------------------------------
    def appendLog(self, message: str):
        self.logMessage.emit(message)

    def setStatus(self, connected: bool):
        self.statusChanged.emit(connected)

    def setMode(self, text: str):
        self.modeChanged.emit(text)

    def setMetrics(self, metrics: dict):
        self.metricsUpdated.emit(json.dumps(metrics, ensure_ascii=False))

    def setServers(self, servers: list[dict]):
        """Обновляет список серверов, сохраняя выбранный сервер если возможно."""
        self.servers = servers
        
        # Пытаемся сохранить выбранный сервер по хосту
        if self._last_selected_host:
            for idx, srv in enumerate(servers):
                if srv.get("host") == self._last_selected_host:
                    self.selected_index = idx
                    self.serversUpdated.emit(json.dumps(servers, ensure_ascii=False))
                    return
        
        # Если сервер не найден, выбираем первый
        self.selected_index = 0 if servers else -1
        self.serversUpdated.emit(json.dumps(servers, ensure_ascii=False))

    def setLinkHistory(self, links: list[str]):
        self.linkHistoryUpdated.emit(json.dumps(links, ensure_ascii=False))
