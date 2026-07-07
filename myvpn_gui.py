     def _on_page_loaded(self, ok: bool):
        """Публикуем начальный список серверов после загрузки страницы."""
        # ВАЖНО: сначала восстанавливаем выбранный сервер ДО setServers()
        # чтобы он не был сброшен на 0
        saved_state = _load_app_state()
        if saved_state.get("last_selected_host"):
            self._bridge._last_selected_host = saved_state["last_selected_host"]
        
        self._bridge.setServers(self._servers)
        self._bridge.setStatus(False)
        self._bridge.setLinkHistory(self._link_history)
