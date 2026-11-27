import ctypes
from collections import Counter
from ctypes import wintypes as win
from dataclasses import dataclass

import uiautomation as uia
from PIL import Image, ImageChops, ImageFilter, ImageGrab

from wechat import ENUM_PROC, MAIN_TITLE, USER32


USER32.EnumChildWindows.argtypes = (win.HWND, ENUM_PROC, win.LPARAM)
USER32.GetClassNameW.argtypes = (win.HWND, win.LPWSTR, ctypes.c_int)
USER32.GetWindowRect.argtypes = (win.HWND, ctypes.POINTER(win.RECT))
USER32.GetWindowDisplayAffinity.argtypes = (win.HWND, ctypes.POINTER(win.DWORD))
USER32.GetDpiForWindow.argtypes = (win.HWND,)
USER32.GetDpiForWindow.restype = win.UINT


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def from_rect(cls, value):
        return cls(value.left, value.top, value.right, value.bottom)

    @property
    def size(self):
        return self.right - self.left, self.bottom - self.top

    def contains(self, box):
        return self.left <= box.left and self.top <= box.top and self.right >= box.right and self.bottom >= box.bottom

    def relative_to(self, box):
        return self.left - box.left, self.top - box.top, self.right - box.left, self.bottom - box.top


def window_bounds(handle):
    rect = win.RECT()
    return Box.from_rect(rect) if USER32.GetWindowRect(handle, ctypes.byref(rect)) else None


def render_child(parent, target):
    windows = []

    @ENUM_PROC
    def collect(handle, _):
        name = ctypes.create_unicode_buffer(80)
        USER32.GetClassNameW(handle, name, len(name))
        if name.value.startswith("MMUIRenderSubWindow") and USER32.IsWindowVisible(handle):
            bounds = window_bounds(handle)
            if bounds and bounds.contains(target):
                windows.append((handle, bounds))
        return True

    USER32.EnumChildWindows(parent, collect, 0)
    return min(windows, key=lambda item: item[1].size[0] * item[1].size[1]) if windows else (None, None)


def components(mask, minimum):
    width, height = mask.size
    pixels = bytearray(mask.tobytes())
    for index, value in enumerate(pixels):
        if not value:
            continue
        stack = [index]
        pixels[index] = 0
        top, left = divmod(index, width)
        bottom, right, count = top, left, 0
        while stack:
            point = stack.pop()
            y, x = divmod(point, width)
            top, bottom = min(top, y), max(bottom, y)
            left, right = min(left, x), max(right, x)
            count += 1
            for neighbor, valid in ((point - 1, x > 0), (point + 1, x + 1 < width),
                                    (point - width, y > 0), (point + width, y + 1 < height)):
                if valid and pixels[neighbor]:
                    pixels[neighbor] = 0
                    stack.append(neighbor)
        span_x, span_y = right - left + 1, bottom - top + 1
        if min(span_x, span_y) >= minimum and count >= span_x * span_y * 0.30:
            yield count, (left, top, right + 1, bottom + 1)


def isolate_picture(row, full_width, dpi):
    if min(row.size) < 28 or not any(high - low > 7 for low, high in row.getextrema()):
        return None
    if not full_width:
        return row

    # Wide accessibility rows also contain the avatar and sender label. Keep only a distinct picture-sized region.
    gutter = min(round(55 * dpi / 96), row.width // 7)
    content = row.crop((gutter, 0, row.width - gutter, row.height))
    ratio = min(1.0, 384 / max(content.size))
    size = tuple(max(1, round(side * ratio)) for side in content.size)
    sample = content.resize(size, Image.Resampling.BILINEAR)
    background, count = Counter(sample.getdata()).most_common(1)[0]
    if count < sample.width * sample.height * 0.09:
        return None
    red, green, blue = ImageChops.difference(sample, Image.new("RGB", sample.size, background)).split()
    difference = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = difference.point(lambda value: 255 if value >= 8 else 0).filter(ImageFilter.MaxFilter(3))
    regions = sorted(components(mask, max(13, round(26 * (dpi / 96) * ratio))), reverse=True)
    if not regions or (len(regions) > 1 and regions[1][0] > regions[0][0] * 0.42):
        return None
    left, top, right, bottom = regions[0][1]
    if left == 0 or right == sample.width or top == 0 or bottom == sample.height:
        return None
    return content.crop((int(left / ratio), int(top / ratio), round(right / ratio), round(bottom / ratio)))


class PhotoExporter:
    def __init__(self, reader):
        self.reader = reader

    def location(self, window, group, row):
        root = self.reader.api.ElementFromHandle(window.handle)
        if MAIN_TITLE.fullmatch(window.title):
            if self.reader.active_group(root) != group:
                return None
        elif uia.GetWindowText(window.handle).strip() != window.title:
            return None
        chat, elements = self.reader.elements(root)
        if elements is None:
            return None
        chat_box = Box.from_rect(chat.CurrentBoundingRectangle)
        for index in range(max(0, elements.Length - 250), elements.Length):
            element = elements.GetElement(index)
            if self.reader.row(element) == row and not element.CurrentIsOffscreen:
                bounds = Box.from_rect(element.CurrentBoundingRectangle)
                if chat_box.contains(bounds) and min(bounds.size) >= 28:
                    return bounds, bounds.size[0] >= chat_box.size[0] * 0.75
        return None

    def export(self, window, group, row, directory, stop):
        if not window or stop.is_set() or uia.IsIconic(window.handle):
            return None
        pid = win.DWORD()
        USER32.GetWindowThreadProcessId(window.handle, ctypes.byref(pid))
        affinity = win.DWORD()
        if (pid.value != window.pid or not USER32.GetWindowDisplayAffinity(window.handle, ctypes.byref(affinity))
                or affinity.value):
            return None
        location = self.location(window, group, row)
        if not location:
            return None
        box, wide = location
        child, frame_box = render_child(window.handle, box)
        if not child:
            return None
        with ImageGrab.grab(window=child) as frame:
            if frame.size != frame_box.size or window_bounds(child) != frame_box or stop.is_set():
                return None
            if location != self.location(window, group, row):
                return None
            cropped = isolate_picture(frame.crop(box.relative_to(frame_box)), wide, USER32.GetDpiForWindow(window.handle) or 96)
            if not cropped:
                return None
            path = directory / "photo.png"
            cropped.save(path, "PNG")
            return path
