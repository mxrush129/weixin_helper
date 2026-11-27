import ctypes
import tempfile
from ctypes import wintypes
from pathlib import Path


KEY_FILENAME = "deepseek-key.dpapi"
UI_FORBIDDEN = 0x01


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
blob_pointer = ctypes.POINTER(DataBlob)
common_arguments = (blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer)
crypt32.CryptProtectData.argtypes = (blob_pointer, wintypes.LPCWSTR, *common_arguments)
crypt32.CryptUnprotectData.argtypes = (blob_pointer, ctypes.POINTER(wintypes.LPWSTR), *common_arguments)
crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
kernel32.LocalFree.restype = wintypes.HLOCAL


def dpapi(operation, data):
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), source_buffer)
    result = DataBlob()
    try:
        if not operation(ctypes.byref(source), None, None, None, None, UI_FORBIDDEN, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(source_buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel32.LocalFree(ctypes.cast(result.data, wintypes.HLOCAL))


def save_key(path, key):
    if not key:
        raise ValueError("API 密钥不能为空。")
    encrypted = dpapi(crypt32.CryptProtectData, key.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="key-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encrypted)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def load_key(path):
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        return ""
    return dpapi(crypt32.CryptUnprotectData, encrypted).decode("utf-8")
