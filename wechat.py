import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass

import uiautomation as uia
from comtypes import COMError
from uiautomation.uiautomation import _AutomationClient


MAIN_TITLE = re.compile(r"(?:微信|Weixin|WeChat)(?:[（(]\d+[）)])?", re.IGNORECASE)
MEMBER_COUNT = re.compile(r"\s*[（(]\d+[）)]$")
TIMESTAMP = re.compile(
    r"(?:(?:今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]|\d{1,4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?)\s*)?"
    r"(?:(?:上午|下午|晚上|凌晨)\s*)?\d{1,2}:\d{2}(?:\s*[AP]M)?",
    re.IGNORECASE,
)
RECALL = re.compile(
    r"(?:.{1,160}(?:撤回了一条消息|撤回了一則訊息|recalled a message|withdrew a message))"
    r"(?:\s*(?:重新编辑|重新編輯|re-edit))?[。.]?",
    re.IGNORECASE | re.DOTALL,
)
IMAGE_LABEL = re.compile(r"(?:\[|【)?(?:图片|圖片|照片|image|photo)(?:\]|】)?", re.IGNORECASE)
IMAGE_CLASS = re.compile(r"chat.*(?:image|picture).*view", re.IGNORECASE)

USER32 = ctypes.WinDLL("user32", use_last_error=True)
ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
USER32.EnumWindows.argtypes = (ENUM_PROC, wintypes.LPARAM)
USER32.IsWindowVisible.argtypes = (wintypes.HWND,)
USER32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


@dataclass(frozen=True)
class Row:
    runtime_id: tuple[int, ...]
    kind: str
    text: str

    @property
    def signature(self):
        return self.kind, self.text


@dataclass(frozen=True)
class Window:
    handle: int
    pid: int
    title: str


@dataclass(frozen=True)
class Snapshot:
    source: str
    rows: list[Row]
    window: Window | None = None


def wechat_windows():
    pids = {p.pid for p in uia.GetProcesses(False) if p.exeName.lower() in {"weixin.exe", "wechat.exe"}}
    windows = []

    @ENUM_PROC
    def collect(handle, _):
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value in pids and USER32.IsWindowVisible(handle):
            title = uia.GetWindowText(handle).strip()
            if title:
                windows.append(Window(handle, pid.value, title))
        return True

    if pids:
        USER32.EnumWindows(collect, 0)
    return windows


def row_kind(body, class_name, automation_id):
    if IMAGE_LABEL.fullmatch(body) or IMAGE_CLASS.search(class_name):
        return "image"
    if "bubble" in (class_name + automation_id).lower():
        return "message"
    if RECALL.fullmatch(body):
        return "recall"
    if TIMESTAMP.fullmatch(body):
        return "time"
    return "message"


class WeChatReader:
    # UI Automation TreeScope_Descendants.
    DESCENDANTS = 4

    def __init__(self):
        client = _AutomationClient()
        self.api = client.IUIAutomation
        self.interfaces = client.UIAutomationCore
        self.message_list = self.api.CreatePropertyCondition(uia.PropertyId.AutomationIdProperty, "chat_message_list")
        self.input_field = self.api.CreatePropertyCondition(uia.PropertyId.AutomationIdProperty, "chat_input_field")
        self.row_filter = self.api.CreateOrCondition(
            self.api.CreatePropertyCondition(uia.PropertyId.ControlTypeProperty, uia.ControlType.ListItemControl),
            self.api.CreatePropertyCondition(uia.PropertyId.ControlTypeProperty, uia.ControlType.CheckBoxControl),
        )
        self.cache = self.api.CreateCacheRequest()
        for prop in (uia.PropertyId.NameProperty, uia.PropertyId.ClassNameProperty,
                     uia.PropertyId.AutomationIdProperty, uia.PropertyId.RuntimeIdProperty):
            self.cache.AddProperty(prop)

    def active_group(self, root):
        editor = root.FindFirst(self.DESCENDANTS, self.input_field)
        if not editor:
            return ""
        name = editor.CurrentName
        if not name:
            return ""
        named_text = self.api.CreateAndCondition(
            self.api.CreatePropertyCondition(uia.PropertyId.NameProperty, name),
            self.api.CreatePropertyCondition(uia.PropertyId.ControlTypeProperty, uia.ControlType.TextControl),
        )
        names = root.FindAll(self.DESCENDANTS, named_text)
        for index in range(names.Length):
            label = names.GetElement(index)
            if label.CurrentAutomationId.endswith("current_chat_name_label"):
                count = self.api.RawViewWalker.GetNextSiblingElement(label)
                if count and count.CurrentAutomationId.endswith("current_chat_count_label"):
                    if MEMBER_COUNT.fullmatch(count.CurrentName):
                        return name
        return ""

    def elements(self, root):
        chat = root.FindFirst(self.DESCENDANTS, self.message_list)
        return (chat, chat.FindAllBuildCache(self.DESCENDANTS, self.row_filter, self.cache)) if chat else (None, None)

    def row(self, element):
        body = element.CachedName.strip()
        kind = row_kind(body, element.CachedClassName, element.CachedAutomationId)
        if not body and kind != "image":
            return None
        identity = element.GetCachedPropertyValue(uia.PropertyId.RuntimeIdProperty)
        return Row(tuple(identity or ()), kind, body or "[图片]")

    def read_rows(self, root):
        _, elements = self.elements(root)
        if elements is None:
            return None
        rows = []
        for index in range(max(0, elements.Length - 250), elements.Length):
            row = self.row(elements.GetElement(index))
            if row is not None:
                rows.append(row)
        return rows

    def scan(self, names):
        windows = wechat_windows()
        snapshots = {}
        status = {name: "等待微信聊天窗口" for name in names}
        main = []
        candidates = {name: [] for name in names}
        for window in windows:
            if MAIN_TITLE.fullmatch(window.title):
                main.append(window)
                continue
            match = window.title if window.title in candidates else MEMBER_COUNT.sub("", window.title).rstrip()
            if match in candidates:
                candidates[match].append(window)

        for name, matches in candidates.items():
            if len(matches) > 1:
                status[name] = "有重名窗口，请只保留目标群窗口"
            elif matches:
                self.read_window(matches[0], name, False, snapshots, status)

        remaining = {name for name, matches in candidates.items() if not matches}
        for window in main if remaining else ():
            if uia.IsIconic(window.handle):
                continue
            try:
                root = self.api.ElementFromHandle(window.handle)
                name = self.active_group(root)
                if name in remaining:
                    self.read_window(window, name, True, snapshots, status, root)
                    remaining.remove(name)
            except (COMError, OSError, ValueError):
                continue
        return snapshots, status

    def read_window(self, window, name, is_main, snapshots, status, root=None):
        if uia.IsIconic(window.handle):
            status[name] = "聊天窗口已最小化"
            return
        try:
            root = root if root is not None else self.api.ElementFromHandle(window.handle)
            rows = self.read_rows(root)
            if rows is None:
                status[name] = "微信未提供消息列表"
            elif (self.active_group(root) != name if is_main else uia.GetWindowText(window.handle).strip() != window.title):
                status[name] = "聊天已切换，等待下次采集"
            else:
                snapshots[name] = Snapshot(f"{window.pid}:{window.handle}", rows, window)
                status[name] = "已连接" if rows else "已连接 · 暂无可读消息"
        except (COMError, OSError, ValueError):
            status[name] = "暂时无法读取微信窗口"
