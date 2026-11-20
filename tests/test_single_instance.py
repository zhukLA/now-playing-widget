"""Тесты single-instance (QLocalServer/QLocalSocket) из main.py."""

from __future__ import annotations

import os
import time
import uuid

import pytest


@pytest.fixture
def server_name() -> str:
    return f"npw-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _pump(qapp, condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_show_command_reaches_running_instance(qapp, server_name) -> None:
    import main

    server, already_running = main._create_instance_server(server_name)
    assert server is not None and already_running is False
    try:
        shown: list[bool] = []
        main._attach_show_handler(server, lambda: shown.append(True))

        assert main._send_show_command(server_name) is True
        # Команда приходит, даже если байты успели прийти до newConnection
        # (раньше первый readAll() «съедал» её, и окно не поднималось).
        assert _pump(qapp, lambda: shown == [True])
    finally:
        server.close()


def test_second_instance_detects_running_one(qapp, server_name) -> None:
    import main

    first, _ = main._create_instance_server(server_name)
    assert first is not None
    shown: list[bool] = []
    main._attach_show_handler(first, lambda: shown.append(True))
    try:
        second, already_running = main._create_instance_server(server_name)
        # На Unix второй listen на занятое имя невозможен — второй экземпляр
        # должен понять, что первый жив, и НЕ удалять его сокет.
        if second is None:
            assert already_running is True
            assert _pump(qapp, lambda: shown == [True])
            # Первый экземпляр всё ещё принимает команды.
            assert main._send_show_command(server_name) is True
            assert _pump(qapp, lambda: len(shown) == 2)
        else:  # pragma: no cover — Windows: именованный канал допускает 2-й listen
            second.close()
    finally:
        first.close()


def test_no_server_means_send_fails(qapp, server_name) -> None:
    import main

    assert main._send_show_command(server_name, timeout_ms=100) is False
