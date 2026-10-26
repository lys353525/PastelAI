"""Windows Credential Manager-backed API key storage."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
SERVICE_NAME = "PastelAIStudio"
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    """Raised when the OS credential store cannot be accessed safely."""


def _windows_api():
    if os.name != "nt":
        raise CredentialError("API 키는 Windows 자격 증명 저장소에서만 사용할 수 있습니다.")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    class CREDENTIALW(ctypes.Structure):
        pass
    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    CREDENTIALW._fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]
    advapi.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi, CREDENTIALW, PCREDENTIALW


def _target(provider: str) -> str:
    if provider not in {"api", "claude"}:
        raise ValueError("지원하지 않는 API 제공자입니다.")
    return f"{SERVICE_NAME}/{provider}"


def set_api_key(provider: str, value: str) -> None:
    advapi, CREDENTIALW, _ = _windows_api()
    target = _target(provider)
    if not value:
        delete_api_key(provider)
        return
    blob = value.encode("utf-16-le")
    storage = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(storage, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = provider
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise CredentialError(f"Windows 자격 증명 저장 실패 (오류 {ctypes.get_last_error()}).")


def get_api_key(provider: str) -> str:
    advapi, _, PCREDENTIALW = _windows_api()
    credential = PCREDENTIALW()
    if not advapi.CredReadW(_target(provider), CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return ""
        raise CredentialError(f"Windows 자격 증명 읽기 실패 (오류 {code}).")
    try:
        item = credential.contents
        raw = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        advapi.CredFree(credential)


def delete_api_key(provider: str) -> None:
    advapi, _, _ = _windows_api()
    if not advapi.CredDeleteW(_target(provider), CRED_TYPE_GENERIC, 0):
        code = ctypes.get_last_error()
        if code != ERROR_NOT_FOUND:
            raise CredentialError(f"Windows 자격 증명 삭제 실패 (오류 {code}).")
