"""Системная громкость Windows (мастер-громкость устройства вывода по умолчанию).

Без внешних зависимостей (pycaw/comtypes не нужны): минимальный COM-вызов
через ``ctypes`` — ``IMMDeviceEnumerator`` → ``IAudioEndpointVolume``.
Если что-то не получилось (не Windows, нет аудиоустройства) — ``available``
остаётся False, а get/set безопасно ничего не делают.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_void_p
from typing import Optional

logger = logging.getLogger("npw.audio")

_CLSCTX_ALL = 23
_E_RENDER = 0
_E_MULTIMEDIA = 1


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    import uuid  # noqa: PLC0415

    u = uuid.UUID(text)
    guid = _GUID()
    guid.Data1 = u.time_low
    guid.Data2 = u.time_mid
    guid.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        guid.Data4[i] = b
    return guid


_CLSID_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_IID_ENDPOINT_VOLUME = "5CDF2C82-841E-4546-9722-0CF74078229A"

# Индексы методов в vtable (IUnknown занимает 0..2).
_ENUM_GET_DEFAULT = 4  # IMMDeviceEnumerator::GetDefaultAudioEndpoint
_DEV_ACTIVATE = 3  # IMMDevice::Activate
_VOL_SET_SCALAR = 7  # IAudioEndpointVolume::SetMasterVolumeLevelScalar
_VOL_GET_SCALAR = 9  # IAudioEndpointVolume::GetMasterVolumeLevelScalar
_VOL_SET_MUTE = 14
_VOL_GET_MUTE = 15


def _method(obj: c_void_p, index: int, *argtypes):
    """Функция из vtable COM-объекта (первый аргумент — сам объект)."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)
    return proto(vtable[index])


def _release(obj: Optional[c_void_p]) -> None:
    if obj:
        try:
            _method(obj, 2)(obj)
        except Exception:  # noqa: BLE001
            logger.debug("Release не удался", exc_info=True)


class MasterVolume:
    """Мастер-громкость: ``get()`` → 0..1, ``set(0..1)``, ``muted``/``set_muted``."""

    def __init__(self) -> None:
        self._endpoint: Optional[c_void_p] = None
        if sys.platform != "win32":
            return
        try:
            self._endpoint = self._open_endpoint()
        except Exception:  # noqa: BLE001
            logger.warning("системная громкость недоступна", exc_info=True)
            self._endpoint = None

    @staticmethod
    def _open_endpoint() -> Optional[c_void_p]:
        ole32 = ctypes.windll.ole32
        # Qt обычно уже инициализировал COM в этом потоке; S_FALSE и
        # RPC_E_CHANGED_MODE здесь не ошибки.
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        enumerator = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(_guid(_CLSID_ENUMERATOR)),
            None,
            _CLSCTX_ALL,
            byref(_guid(_IID_ENUMERATOR)),
            byref(enumerator),
        )
        if hr != 0 or not enumerator:
            raise OSError(f"CoCreateInstance: 0x{hr & 0xFFFFFFFF:08X}")
        device = c_void_p()
        try:
            hr = _method(enumerator, _ENUM_GET_DEFAULT, c_int, c_int, POINTER(c_void_p))(
                enumerator, _E_RENDER, _E_MULTIMEDIA, byref(device)
            )
            if hr != 0 or not device:
                raise OSError(f"GetDefaultAudioEndpoint: 0x{hr & 0xFFFFFFFF:08X}")
        finally:
            _release(enumerator)
        endpoint = c_void_p()
        try:
            hr = _method(
                device, _DEV_ACTIVATE, POINTER(_GUID), c_uint, c_void_p, POINTER(c_void_p)
            )(device, byref(_guid(_IID_ENDPOINT_VOLUME)), _CLSCTX_ALL, None, byref(endpoint))
            if hr != 0 or not endpoint:
                raise OSError(f"Activate: 0x{hr & 0xFFFFFFFF:08X}")
        finally:
            _release(device)
        return endpoint

    @property
    def available(self) -> bool:
        return self._endpoint is not None

    def get(self) -> Optional[float]:
        """Текущая громкость 0..1 или None, если недоступна."""
        ep = self._endpoint
        if ep is None:
            return None
        value = c_float()
        try:
            hr = _method(ep, _VOL_GET_SCALAR, POINTER(c_float))(ep, byref(value))
        except Exception:  # noqa: BLE001
            logger.debug("GetMasterVolumeLevelScalar", exc_info=True)
            return None
        return float(value.value) if hr == 0 else None

    def set(self, level: float) -> bool:
        ep = self._endpoint
        if ep is None:
            return False
        level = max(0.0, min(1.0, float(level)))
        try:
            hr = _method(ep, _VOL_SET_SCALAR, c_float, c_void_p)(ep, c_float(level), None)
        except Exception:  # noqa: BLE001
            logger.debug("SetMasterVolumeLevelScalar", exc_info=True)
            return False
        return hr == 0

    @property
    def muted(self) -> bool:
        ep = self._endpoint
        if ep is None:
            return False
        flag = c_int()
        try:
            hr = _method(ep, _VOL_GET_MUTE, POINTER(c_int))(ep, byref(flag))
        except Exception:  # noqa: BLE001
            return False
        return hr == 0 and bool(flag.value)

    def set_muted(self, muted: bool) -> bool:
        ep = self._endpoint
        if ep is None:
            return False
        try:
            hr = _method(ep, _VOL_SET_MUTE, c_int, c_void_p)(ep, int(bool(muted)), None)
        except Exception:  # noqa: BLE001
            return False
        return hr == 0
