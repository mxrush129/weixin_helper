import ctypes
import os
import queue
import threading
import time
import tkinter as tk
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

import uiautomation as uia
from comtypes import COMError

from archive import Archive
from credentials import KEY_FILENAME, load_key, save_key
from photo_export import PhotoExporter
from pictures import archive_picture
from summary import DEEPSEEK_HOST, DEFAULT_MODEL, DEFAULT_URL, LOCAL_HOSTS, analyze_image, completion_url, summarize, supports_vision
from wechat import WeChatReader


DATA = Path(__file__).resolve().parent / "data" / "wechat.sqlite3"
INTERVALS = ("0.5", "1", "2", "5", "10", "30", "60")
HOURS = ("1", "6", "24", "72", "168")


def format_time(value, style="%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(value).strftime(style)


class Helper:
    def __init__(self, root, path=DATA):
        self.root = root
        self.data = path
        self.archive = Archive(path)
        self.events = queue.Queue()
        self.groups = self.archive.get_setting("groups", [])
        self.watch = (tuple(self.groups), float(self.archive.get_setting("interval", "1")))
        self.stop = None
        self.statuses = {}
        self.summary_busy = False
        self.close_pending = False
        self.picture_dir = path.parent / "images"
        self.picture_work = ()
        self.picture_failures = set()
        self.picture_status = None
        self.save_pictures = self.archive.get_setting("auto_images", True)
        self.link_tags = {}
        self.url = self.archive.get_setting("api_url", DEFAULT_URL)
        self.model = self.archive.get_setting("model", DEFAULT_MODEL)
        self.key = ""
        self.key_file = self.data.parent / KEY_FILENAME

        root.title("微信群聊助手")
        root.geometry("1020x690")
        root.minsize(830, 570)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.configure_style()
        self.build()
        self.fill_groups(self.groups[0] if self.groups else None)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(125, self.poll_events)

    def configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background="#f5f7fa", foreground="#182738", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Muted.TLabel", foreground="#4c6074")
        style.configure("TButton", padding=(11, 6))
        style.configure("Primary.TButton", background="#0c6956", foreground="white", bordercolor="#0c6956")
        style.map("Primary.TButton", background=[("disabled", "#d9e7e3"), ("active", "#085243")],
                  foreground=[("disabled", "#526963")])
        style.configure("TNotebook.Tab", padding=(14, 7))
        self.root.configure(background="#f5f7fa")

    def build(self):
        view = ttk.Frame(self.root, padding=(20, 15, 20, 12))
        view.pack(fill="both", expand=True)
        view.columnconfigure(1, weight=1)
        view.rowconfigure(2, weight=1)

        header = ttk.Frame(view)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 13))
        ttk.Label(header, text="微信群聊助手", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="模型设置", command=self.open_settings).pack(side="right")
        self.monitor_button = ttk.Button(header, text="开始监控", style="Primary.TButton", command=self.toggle_monitor)
        self.monitor_button.pack(side="right", padx=(0, 8))
        self.interval = tk.StringVar(value=str(self.archive.get_setting("interval", "1")))
        ttk.Label(header, text="秒").pack(side="right", padx=(4, 14))
        interval_box = ttk.Combobox(header, textvariable=self.interval, values=INTERVALS, width=4, state="readonly")
        interval_box.pack(side="right")
        interval_box.bind("<<ComboboxSelected>>", self.change_interval)
        ttk.Label(header, text="采集间隔").pack(side="right", padx=(0, 7))

        self.monitor_status = tk.StringVar(value="监控已暂停")
        ttk.Label(view, textvariable=self.monitor_status, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 13))

        sidebar = ttk.Frame(view, width=228)
        sidebar.grid(row=2, column=0, sticky="ns", padx=(0, 16))
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(4, weight=1)
        ttk.Label(sidebar, text="监控群聊", style="Heading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.group_name = tk.StringVar()
        group_entry = ttk.Entry(sidebar, textvariable=self.group_name)
        group_entry.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        group_entry.bind("<Return>", lambda _: self.add_group())
        ttk.Button(sidebar, text="添加群聊", command=self.add_group).grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(sidebar, text="输入与微信一致的完整群名。\n在微信会话列表双击群聊，打开独立窗口。",
                  wraplength=224, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 10))
        self.group_box = tk.Listbox(sidebar, background="white", foreground="#182738", activestyle="none",
                                    selectbackground="#dbece6", selectforeground="#143e34", relief="flat",
                                    highlightthickness=1, highlightbackground="#cad5df", exportselection=False)
        self.group_box.grid(row=4, column=0, sticky="nsew", pady=(0, 7))
        self.group_box.bind("<<ListboxSelect>>", lambda _: self.refresh_group())
        ttk.Button(sidebar, text="移除监控", command=self.remove_group).grid(row=5, column=0, sticky="ew")

        pane = ttk.Frame(view)
        pane.grid(row=2, column=1, sticky="nsew")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(2, weight=1)
        self.group_title = tk.StringVar()
        ttk.Label(pane, textvariable=self.group_title, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.group_status = tk.StringVar()
        ttk.Label(pane, textvariable=self.group_status, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 8))

        self.tabs = ttk.Notebook(pane)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        messages = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(messages, text="消息记录")
        messages.columnconfigure(0, weight=1)
        messages.rowconfigure(1, weight=1)
        self.message_info = tk.StringVar()
        ttk.Label(messages, textvariable=self.message_info, style="Muted.TLabel").grid(sticky="w", pady=(0, 6))
        self.messages = self.text_panel(messages, 1)
        self.messages.tag_configure("meta", foreground="#506a80", spacing1=5)
        self.messages.tag_configure("recall", foreground="#8d4a12")
        self.messages.tag_configure("body", spacing3=5)
        self.messages.tag_configure("link", foreground="#0c6956", underline=True)
        self.messages.tag_bind("link", "<Enter>", lambda _: self.messages.configure(cursor="hand2"))
        self.messages.tag_bind("link", "<Leave>", lambda _: self.messages.configure(cursor="xterm"))

        self.summary_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.summary_tab, text="AI 总结")
        self.summary_tab.columnconfigure(0, weight=1)
        self.summary_tab.rowconfigure(3, weight=1)
        actions = ttk.Frame(self.summary_tab)
        actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(actions, text="最近").pack(side="left", padx=(0, 5))
        self.hours = tk.StringVar(value="24")
        ttk.Combobox(actions, textvariable=self.hours, values=HOURS, width=4, state="readonly").pack(side="left")
        ttk.Label(actions, text="小时").pack(side="left", padx=(5, 10))
        self.summary_button = ttk.Button(actions, text="生成 AI 总结", style="Primary.TButton", command=self.start_summary)
        self.summary_button.pack(side="left")
        self.model_info = tk.StringVar()
        ttk.Label(self.summary_tab, textvariable=self.model_info, style="Muted.TLabel", wraplength=520).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.summary_info = tk.StringVar()
        ttk.Label(self.summary_tab, textvariable=self.summary_info, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.summary_text = self.text_panel(self.summary_tab, 3)
        self.auto_images = tk.BooleanVar(value=self.save_pictures)
        ttk.Checkbutton(view, text="自动保存图片截图", variable=self.auto_images, command=self.change_auto_images).grid(
            row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(view, text="已保存的内容会留在本机。撤回太快时，可能来不及采集。",
                  style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=(10, 0))

    def text_panel(self, parent, row):
        box = tk.Text(parent, wrap="word", background="white", foreground="#182738", relief="flat",
                      padx=10, pady=7, width=1, height=1, highlightthickness=1, highlightbackground="#cad5df",
                      state="disabled")
        bar = ttk.Scrollbar(parent, command=box.yview)
        box.configure(yscrollcommand=bar.set)
        box.grid(row=row, column=0, sticky="nsew")
        bar.grid(row=row, column=1, sticky="ns")
        return box

    def selected(self):
        selection = self.group_box.curselection()
        return self.groups[selection[0]] if selection else None

    def fill_groups(self, active):
        self.group_box.delete(0, tk.END)
        for name in self.groups:
            self.group_box.insert(tk.END, name)
        if active in self.groups:
            self.group_box.selection_set(self.groups.index(active))
        self.refresh_group()

    def add_group(self):
        name = self.group_name.get().strip()
        if not name:
            return
        if name not in self.groups:
            self.groups.append(name)
            self.archive.save_setting("groups", self.groups)
            self.change_watch()
        self.group_name.set("")
        self.fill_groups(name)

    def remove_group(self):
        name = self.selected()
        if name is None:
            return
        self.groups.remove(name)
        self.archive.save_setting("groups", self.groups)
        self.change_watch()
        self.fill_groups(self.groups[0] if self.groups else None)

    def change_interval(self, _event=None):
        self.archive.save_setting("interval", self.interval.get())
        self.change_watch()

    def change_watch(self):
        self.watch = (tuple(self.groups), float(self.interval.get()))

    def change_auto_images(self):
        self.save_pictures = self.auto_images.get()
        self.archive.save_setting("auto_images", self.save_pictures)

    def refresh_group(self):
        name = self.selected()
        self.group_title.set(name or "请添加一个群聊")
        self.refresh_connection()
        self.render_messages()
        self.render_summary()
        self.update_model_info()
        self.summary_button.configure(state="normal" if name and not self.summary_busy else "disabled")

    def refresh_connection(self):
        if self.selected() is None:
            self.group_status.set("在左侧添加群名后开始监控。")
        elif self.stop is None:
            self.group_status.set("监控已暂停")
        else:
            self.group_status.set(self.statuses.get(self.selected(), "等待首次采集"))

    def render_messages(self, keep_scroll=False):
        name = self.selected()
        rows = self.archive.recent(name) if name else []
        info = f"最近 {len(rows)} 条 · 按采集时间显示" if rows else "暂无已采集消息"
        self.message_info.set(self.picture_status or info)
        box = self.messages
        old_view = box.yview()
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        if self.link_tags:
            for tag, binding in self.link_tags.items():
                box.tag_unbind(tag, "<Button-1>", binding)
            box.tag_delete(*self.link_tags)
            self.link_tags.clear()
        for row in rows:
            kind = "  撤回提示" if row["kind"] == "recall" else ""
            label = f"  微信 {row['wechat_time']}" if row["wechat_time"] else ""
            box.insert(tk.END, f"[{row['id']}]  {format_time(row['captured_at'])}{label}{kind}\n", "meta")
            if row["kind"] == "image":
                self.render_picture(name, row)
            else:
                box.insert(tk.END, row["body"] + "\n", ("body", "recall") if kind else "body")
        box.configure(state="disabled")
        box.yview_moveto(old_view[0] if keep_scroll and old_view[1] < 0.999 else 1.0)

    def picture_link(self, label, action):
        tag = f"picture_link_{len(self.link_tags)}"
        self.messages.insert(tk.END, label, ("link", tag))
        self.link_tags[tag] = self.messages.tag_bind(tag, "<Button-1>", lambda _: action())

    def render_picture(self, group, row):
        identifier = row["id"]
        if row["filename"] and (self.picture_dir / row["filename"]).is_file():
            self.messages.insert(tk.END, "图片已保存  ·  ", "body")
            self.picture_link("打开图片", lambda: self.open_picture(row["filename"]))
            if row["description"]:
                self.messages.insert(tk.END, "\n" + row["description"], "body")
            else:
                self.messages.insert(tk.END, "  ·  ")
                self.picture_link("解析图片", lambda: self.start_picture_analysis(group, row))
        else:
            self.messages.insert(tk.END, "图片待保存  ·  ", "body")
            if identifier in self.picture_failures:
                self.messages.insert(tk.END, "暂未取得图片画面  ·  ")
            self.picture_link("导入图片", lambda: self.import_picture(group, identifier))
        self.messages.insert(tk.END, "\n", "body")

    def open_picture(self, filename):
        try:
            os.startfile(self.picture_dir / filename)
        except OSError as error:
            messagebox.showerror("无法打开图片", str(error), parent=self.root)

    def import_picture(self, group, identifier):
        current = self.archive.image(identifier)
        if not current or (current["filename"] and (self.picture_dir / current["filename"]).is_file()):
            return
        selected = filedialog.askopenfilename(parent=self.root, title="选择这条消息对应的图片",
                                               filetypes=[("图片", "*.jpg *.jpeg *.png *.gif *.webp *.bmp *.tif *.tiff")])
        if not selected:
            return
        try:
            filename = archive_picture(Path(selected), self.picture_dir)
            self.archive.save_image(identifier, filename, current["filename"])
            if self.selected() == group:
                self.render_messages(keep_scroll=True)
        except (OSError, ValueError) as error:
            messagebox.showerror("无法保存图片", str(error), parent=self.root)

    def render_summary(self):
        row = self.archive.latest_summary(self.selected()) if self.selected() else None
        box = self.summary_text
        box.configure(state="normal")
        box.delete("1.0", tk.END)
        if row:
            box.insert(tk.END, row["body"])
        box.configure(state="disabled")
        if not self.summary_busy:
            self.summary_info.set(
                f"{format_time(row['created_at'])}  ·  最近 {row['hours']} 小时  ·  {row['message_count']} 条"
                if row else "暂无总结")

    def update_model_info(self):
        host = urlsplit(self.url).hostname
        if host in LOCAL_HOSTS:
            self.model_info.set(f"使用本机模型接口 · {self.model}")
        else:
            content = "消息和待解析图片" if supports_vision(self.url, self.model) else "消息"
            self.model_info.set(f"生成时会将所选时段的{content}发送至 {host} · {self.model}")

    def toggle_monitor(self):
        if self.stop is not None:
            self.stop.set()
            self.monitor_button.configure(text="正在暂停…", state="disabled")
            return
        if not self.groups:
            messagebox.showinfo("请先添加群聊", "输入微信里的完整群名，再点添加群聊。", parent=self.root)
            return
        self.stop = threading.Event()
        self.statuses = {}
        self.picture_work = ()
        self.picture_status = None
        self.monitor_button.configure(text="暂停监控")
        self.monitor_status.set("正在连接微信…")
        self.refresh_connection()
        threading.Thread(target=self.monitor, args=(self.stop,), daemon=True).start()

    def monitor(self, stop):
        stop_pictures = threading.Event()
        pictures = threading.Thread(target=self.capture_pictures, args=(stop_pictures,), daemon=True)
        pictures.start()
        try:
            with uia.UIAutomationInitializerInThread(), closing(Archive(self.data)) as archive:
                reader = WeChatReader()
                while not stop.is_set():
                    groups, interval = self.watch
                    started = time.monotonic()
                    snapshots, status = reader.scan(groups) if groups else ({}, {})
                    changed = {name for name, snapshot in snapshots.items() if archive.capture(name, snapshot)}
                    self.picture_work = tuple((name, snapshot) for name, snapshot in snapshots.items()
                                              if any(row.kind == "image" for row in snapshot.rows))
                    self.events.put(("scan", (status, changed, len(snapshots))))
                    stop.wait(max(0.1, interval - (time.monotonic() - started)))
        except Exception as error:
            self.events.put(("monitor_error", str(error)))
        finally:
            stop_pictures.set()
            pictures.join(timeout=5)
            self.events.put(("monitor_finished", None))

    def capture_pictures(self, stop):
        retry_at = {}
        exporter = None
        try:
            with uia.UIAutomationInitializerInThread(), closing(Archive(self.data)) as archive:
                while not stop.wait(0.25):
                    work = self.picture_work
                    if not self.save_pictures or not work:
                        continue
                    if exporter is None:
                        exporter = PhotoExporter(WeChatReader())
                    now = time.monotonic()
                    retry_at = {identifier: when for identifier, when in retry_at.items() if when > now}
                    for group, snapshot in work:
                        if stop.is_set() or not self.save_pictures or group not in self.watch[0]:
                            continue
                        job = next(((identifier, row) for identifier, row in archive.pending_images(group, snapshot)
                                    if identifier not in retry_at), None)
                        if not job:
                            continue
                        identifier, row = job
                        with TemporaryDirectory(prefix="weixin-picture-") as temp:
                            try:
                                exported = exporter.export(snapshot.window, group, row, Path(temp), stop)
                                if exported:
                                    filename = archive_picture(exported, self.picture_dir)
                                    if archive.save_image(identifier, filename):
                                        self.events.put(("picture_saved", (group, identifier)))
                                    continue
                            except (COMError, OSError, ValueError):
                                pass
                        retry_at[identifier] = time.monotonic() + 10
                        self.events.put(("picture_retry", (group, identifier)))
        except Exception as error:
            self.events.put(("picture_worker_error", str(error)))

    def open_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("模型设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)
        url, model = (tk.StringVar(value=value) for value in (self.url, self.model))
        key = tk.StringVar()
        for row, (label, variable) in enumerate((("API 地址", url), ("模型名称", model), ("API 密钥", key))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            ttk.Entry(form, textvariable=variable, width=43, show="●" if row == 2 else "").grid(
                row=row, column=1, sticky="ew", pady=4)
        ttk.Label(form, text="DeepSeek 密钥会由 Windows 加密保存在本机。留空沿用已保存的密钥。",
                  style="Muted.TLabel", wraplength=410).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 9))

        def save():
            endpoint, chosen_model, entered_key = url.get().strip(), model.get().strip(), key.get().strip()
            try:
                completion_url(endpoint)
                if not chosen_model:
                    raise ValueError("请输入模型服务提供的模型名称。")
                host = urlsplit(endpoint).hostname
                if host == DEEPSEEK_HOST and entered_key:
                    save_key(self.key_file, entered_key)
            except OSError:
                messagebox.showerror("模型设置", "Windows 无法加密保存密钥。", parent=dialog)
                return
            except ValueError as error:
                messagebox.showerror("模型设置", str(error), parent=dialog)
                return
            if entered_key or host != urlsplit(self.url).hostname:
                self.key = entered_key
            self.url, self.model = endpoint, chosen_model
            self.archive.save_setting("api_url", self.url)
            self.archive.save_setting("model", self.model)
            self.update_model_info()
            dialog.destroy()

        ttk.Button(form, text="保存", style="Primary.TButton", command=save).grid(row=4, column=1, sticky="e")
        dialog.grab_set()

    def model_key(self):
        explicit = self.key or os.environ.get("WEIXIN_HELPER_API_KEY", "")
        if explicit:
            return explicit
        host = urlsplit(self.url).hostname
        if host == DEEPSEEK_HOST:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not key:
                try:
                    key = load_key(self.key_file)
                except (OSError, UnicodeError) as error:
                    raise ValueError("Windows 无法读取已保存的 DeepSeek 密钥，请在「模型设置」里重新填写。") from error
            if not key:
                raise ValueError("请先在「模型设置」里填写 DeepSeek API 密钥。")
            return key
        return os.environ.get("OPENAI_API_KEY", "") if host == "api.openai.com" else ""

    def run_model_job(self, group, task, key, picture=False):
        self.summary_busy = True
        self.summary_button.configure(state="disabled")

        def run(url, model):
            error_message = None
            try:
                with closing(Archive(self.data)) as archive:
                    task(archive, url, model, key)
            except Exception as error:
                error_message = str(error)
            self.events.put(("model_finished", (group, picture, error_message)))

        threading.Thread(target=run, args=(self.url, self.model), daemon=True).start()

    def describe_picture(self, archive, row, url, model, key):
        filename = row["filename"]
        description = archive.image_description(filename, model)
        if not description:
            description = analyze_image(self.picture_dir / filename, url, model, key)
        archive.save_image_description(row["id"], filename, description, model)
        return description

    def start_picture_analysis(self, group, row):
        if self.summary_busy:
            return
        try:
            if not supports_vision(self.url, self.model):
                raise ValueError("请在模型设置中选择 DeepSeek Flash 来解析图片。")
            key = self.model_key()
        except ValueError as error:
            messagebox.showerror("图片解析", str(error), parent=self.root)
            return
        self.picture_status = f"正在解析图片 [{row['id']}]…"
        self.message_info.set(self.picture_status)

        def describe(archive, url, model, api_key):
            self.describe_picture(archive, row, url, model, api_key)

        self.run_model_job(group, describe, key, picture=True)

    def start_summary(self):
        name = self.selected()
        if not name or self.summary_busy:
            return
        hours = int(self.hours.get())
        rows = [dict(row) for row in self.archive.messages_since(name, hours)]
        if not rows:
            self.summary_info.set("这个时段没有已采集消息。")
            return
        try:
            key = self.model_key()
        except ValueError as error:
            self.summary_info.set(str(error))
            return
        self.summary_info.set(f"正在总结 {len(rows)} 条消息…")

        def progress(info):
            self.events.put(("summary_progress", info))

        def generate(archive, url, model, key):
            if supports_vision(url, model):
                images = [row for row in rows if row["filename"] and not row["description"]
                          and (self.picture_dir / row["filename"]).is_file()]
                for index, row in enumerate(images, 1):
                    progress(f"正在解析第 {index}/{len(images)} 张图片…")
                    row["description"] = self.describe_picture(archive, row, url, model, key)
            body = summarize(rows, url, model, key, progress)
            archive.save_summary(name, hours, len(rows), body)

        self.run_model_job(name, generate, key)

    def poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "scan":
                    self.statuses, changed, connected = payload
                    self.monitor_status.set(f"已连接 {connected}/{len(self.groups)} 个群 · 最近检查 {format_time(time.time(), '%H:%M:%S')}")
                    self.refresh_connection()
                    if self.selected() in changed:
                        self.render_messages(keep_scroll=True)
                elif event == "monitor_error":
                    self.monitor_status.set(f"监控已停止 · {payload}")
                elif event == "monitor_finished":
                    was_paused = self.stop.is_set()
                    self.stop = None
                    self.monitor_button.configure(text="开始监控", state="normal")
                    if was_paused:
                        self.monitor_status.set("监控已暂停")
                    self.refresh_connection()
                    if self.close_pending:
                        self.close()
                        return
                elif event in ("picture_saved", "picture_retry"):
                    group, identifier = payload
                    was_failed = identifier in self.picture_failures
                    if event == "picture_retry":
                        self.picture_failures.add(identifier)
                    else:
                        self.picture_failures.discard(identifier)
                    if self.selected() == group and (event == "picture_saved" or not was_failed):
                        self.render_messages(keep_scroll=True)
                elif event == "picture_worker_error":
                    self.picture_status = "自动保存图片已停止。可在消息记录中手动导入。"
                    self.message_info.set(self.picture_status)
                elif event == "summary_progress":
                    self.summary_info.set(payload)
                elif event == "model_finished":
                    group, picture, error = payload
                    self.summary_busy = False
                    if picture:
                        self.picture_status = None
                    self.render_summary()
                    if self.selected() == group or picture:
                        self.render_messages(keep_scroll=True)
                    self.summary_button.configure(state="normal" if self.selected() else "disabled")
                    if error:
                        messagebox.showerror("图片解析未完成" if picture else "AI 总结未完成", error, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(125, self.poll_events)

    def close(self):
        if self.stop is not None:
            self.close_pending = True
            self.stop.set()
            return
        self.archive.close()
        self.root.destroy()


if __name__ == "__main__":
    # Let Windows scale the desktop app according to this display's DPI setting.
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    Helper(root)
    root.mainloop()
