# -*- coding: utf-8 -*-
"""
Cafe Business Manager - Mod Manager (universal edition, UI in 12 languages)

What it does:
1. Lists the mods installed in the game's BepInEx/plugins and lets the user
   enable / disable each one by renaming .dll <-> .dll.disabled.
2. Scans BepInEx/config and links every cfg to the mod that created it,
   so parameters can be edited without touching the file by hand.
3. Parameter editor: parses the real cfg (bool / number / enum / text),
   groups entries by [Section], validates ranges, writes values back and
   keeps a one-time .cfg.bak backup for rollback.
4. Detects a missing BepInEx install and can download / install it.
5. The left panel shows the usage guide in the selected language.
"""
import os
import sys
import io
import json
import re
import ssl
import shutil
import zipfile
import tempfile
import threading
import webbrowser
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from i18n import UI, USAGE_TEXT, SUPPORTED_LANGS, LANG_NAMES

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

# Window title is fixed in English so the exe looks the same in every locale.
APP_TITLE = "Cafe Business Manager - Mod Manager"

def log_exc(exc):
    try:
        with open(os.path.join(APP_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(str(exc) + "\n")
    except Exception:
        pass


def load_json(name, default=None):
    p = os.path.join(APP_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log_exc(e)
        return default


def save_json(name, data):
    p = os.path.join(APP_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)



# 全部用户可见界面文案（key -> 语言 -> 文本；花括号占位符用 .format 填充）

def _lang_sheet(lang):
    """返回 UI 字典（当前语言平铺副本）。"""
    return {k: v.get(lang) or v.get("en", "") for k, v in UI.items()}


def _read_lang():
    """从 config.json 读取语言（模块级 messagebox 用）。"""
    cfg = load_json("config.json") or {}
    lang = cfg.get("lang") or "en"
    return lang if lang in SUPPORTED_LANGS else "en"


def _cfg_range(text):
    nums = re.findall(r"[-+]?\d*\.?\d+", text or "")
    if len(nums) >= 2:
        try:
            return (float(nums[0]), float(nums[1]))
        except ValueError:
            pass
    return None


# ---------- 快捷键（KeyboardShortcut）冲突检测 ----------
# BepInEx 把 KeyboardShortcut 序列化成 "F1 + LeftShift"（主键在前，修饰键在后，
# 以「空格 + 空格」连接）。cfg 里同时有 "# Setting type: KeyboardShortcut" 标注。
_HK_ALIAS = {
    "shift": "leftshift", "lshift": "leftshift",
    "control": "leftctrl", "ctrl": "leftctrl", "lctrl": "leftctrl",
    "alt": "leftalt", "lalt": "leftalt",
    "win": "leftcmd", "super": "leftcmd", "meta": "leftcmd",
    "esc": "escape",
}
_HK_NONE = ("", "none", "null")


def parse_hotkey(value):
    """把 cfg 里的快捷键值解析成规范化集合，用于比较。
    空值 / None 返回 None（表示未绑定）。"""
    raw = (value or "").strip().replace("＋", "+")
    if raw.lower() in _HK_NONE:
        return None
    toks = []
    for part in raw.split("+"):
        k = part.strip().replace(" ", "").replace("_", "").lower()
        if not k:
            continue
        toks.append(_HK_ALIAS.get(k, k))
    return frozenset(toks) if toks else None


def _cfg_plugin_present(cfg_path, plugins_dir):
    """判断 cfg 对应的插件是否还装着（cfg 可能是已删除插件留下的残留）。
    先按 GUID（cfg 文件名）在各 dll 里找内嵌字符串，找不到再用文件名模糊匹配。"""
    if not plugins_dir:
        return True
    guid = os.path.splitext(os.path.basename(cfg_path))[0].strip().lower()
    if not guid:
        return True
    needle = guid.encode("utf-16-le")
    stem = guid.rsplit(".", 1)[-1]
    try:
        names = os.listdir(plugins_dir)
    except Exception:
        return True          # 读不到插件目录就不做过滤，宁可多提示也别漏
    dlls = []
    for n in names:
        p = os.path.join(plugins_dir, n)
        if n.lower().endswith(".dll"):
            dlls.append(p)
        elif os.path.isdir(p):
            try:
                for fn in os.listdir(p):
                    if fn.lower().endswith(".dll"):
                        dlls.append(os.path.join(p, fn))
            except Exception:
                pass
    for d in dlls:
        try:
            with open(d, "rb") as f:
                blob = f.read().lower()
            if needle in blob:
                return True
        except Exception:
            continue
    for n in names:                       # 兜底：文件名相似（兼容 guid 被混淆的插件）
        base = os.path.splitext(n)[0].lower()
        if stem and (stem in base or base in stem):
            return True
    return False


def scan_hotkeys(config_dir, plugins_dir=None):
    """扫描目录下全部 cfg，返回 [(cfg_path, key, section, value)]，只取
    KeyboardShortcut 类型的条目。已卸载插件留下的 cfg 会被跳过。"""
    out = []
    try:
        names = sorted(os.listdir(config_dir))
    except Exception:
        return out
    for fn in names:
        low = fn.lower()
        if not low.endswith(".cfg") or low == "bepinex.cfg":
            continue
        path = os.path.join(config_dir, fn)
        if not os.path.isfile(path):
            continue
        if not _cfg_plugin_present(path, plugins_dir):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        section = ""
        is_hk = False
        for raw in lines:
            s = raw.strip()
            if not s:
                continue
            if s.startswith("["):
                section = s.strip("[]").strip()
                is_hk = False
                continue
            if s.startswith("#"):
                lowc = s.lstrip("#").strip().lower()
                if lowc.startswith("setting type:"):
                    is_hk = "keyboardshortcut" in lowc
                continue
            if is_hk and "=" in s:
                k, _, v = s.partition("=")
                out.append((path, k.strip(), section, v.strip()))
                is_hk = False
    return out


def find_hotkey_conflicts(config_dir, cfg_path, new_items, mod_name,
                          name_of=None, label_of=None, plugins_dir=None):
    """new_items: [(key, label, value)]，只传本次要写入的快捷键项。
    返回冲突列表 [{label, value, kind, owner}]，kind=other|self。"""
    new_norms = {}
    for key, label, val in new_items:
        norm = parse_hotkey(val)
        if norm is None:
            continue
        new_norms.setdefault(norm, []).append((key, label, val))

    conflicts = []
    # 1) 本次表单内部两个参数抢同一个键
    for norm, items in new_norms.items():
        for key, label, val in items[1:]:
            conflicts.append({"label": label, "value": val,
                              "kind": "self", "owner": items[0][1]})

    # 2) 与已写在 cfg 里的快捷键（含其他 mod）冲突
    editing = {k for k, _, _ in new_items}
    seen = set()
    for path, key, section, val in scan_hotkeys(config_dir, plugins_dir):
        same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(cfg_path))
        if same and key in editing:
            continue          # 这一项随本次保存被覆盖，不算冲突
        norm = parse_hotkey(val)
        if norm is None or norm not in new_norms:
            continue
        if same:
            kind = "self"
            owner = label_of(key) if label_of else key
        else:
            kind = "other"
            owner = (name_of(path) if name_of else
                     os.path.splitext(os.path.basename(path))[0]) + " / " + key
        for k, label, v in new_norms[norm]:
            sig = (label, v, owner)
            if sig in seen:
                continue
            seen.add(sig)
            conflicts.append({"label": label, "value": v, "kind": kind, "owner": owner})
    return conflicts


def ask_hotkey_conflict(parent, title, body, ok_text, cancel_text, ok_style=None):
    """快捷键冲突确认框。返回 True=仍然保存，False=返回修改。"""
    try:
        dlg = tk.Toplevel(parent)
    except Exception:
        return messagebox.askyesno(title, body, parent=parent)
    dlg.title(title)
    dlg.configure(bg="#ffffff")
    dlg.resizable(False, False)
    res = {"ok": False}
    tk.Label(dlg, text=body, justify="left", anchor="w", bg="#ffffff", fg="#333333",
             font=("Microsoft YaHei UI", 10), wraplength=560).pack(fill="x", padx=18, pady=(16, 12))
    bar = ttk.Frame(dlg)
    bar.pack(fill="x", padx=18, pady=(0, 14))

    def _ok():
        res["ok"] = True
        dlg.destroy()

    ttk.Button(bar, text=cancel_text, command=dlg.destroy).pack(side="right")
    if ok_style:
        ttk.Button(bar, text=ok_text, style=ok_style, command=_ok).pack(side="right", padx=8)
    else:
        ttk.Button(bar, text=ok_text, command=_ok).pack(side="right", padx=8)
    dlg.update_idletasks()
    try:
        pw = parent.winfo_toplevel()
        x = pw.winfo_rootx() + (pw.winfo_width() - dlg.winfo_width()) // 2
        y = pw.winfo_rooty() + (pw.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
    except Exception:
        pass
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
    try:
        dlg.transient(parent.winfo_toplevel())
        dlg.grab_set()
    except Exception:
        pass
    parent.wait_window(dlg)
    return res["ok"]


def _classify_cfg_type(stype, val, choices):
    t = (stype or "").lower()
    if "keyboardshortcut" in t:
        return "hotkey"
    # 有 Acceptable values 时枚举优先：作者可能给布尔类型声明配 A/B/C 之类的命名值，
    # 此时按 bool 渲染会显示不出真实值。先判 choice 才能保证编辑的是作者允许的选项。
    if choices:
        return "choice"
    if "bool" in t:
        return "bool"
    if any(x in t for x in ("int", "float", "single", "double", "decimal")):
        return "num"
    v = str(val).strip().lower()
    if v in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        return "bool"
    try:
        float(v)
        return "num"
    except ValueError:
        return "text"


def parse_cfg_schema(cfg_path):
    """解析 BepInEx 格式 cfg：根据 # Setting type / # Default value /
    # Acceptable value range / # Acceptable values 注释与键值自动提取全部参数。"""
    try:
        with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    out = []
    section = ""
    desc = []
    author_desc = []   # ## Description: ... 作者说明，优先作为描述正文
    stype = ""
    sdef = ""
    srange = None
    schoices = None
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("["):
            section = s.strip("[]").strip()
            continue
        if s.startswith("##"):
            d = s.lstrip("#").strip()
            dl = d.lower()
            # 跳过 BepInEx 自动生成的头部元数据，别塞进参数说明
            if (dl.startswith("settings file was created by plugin")
                    or dl.startswith("plugin guid:")
                    or dl.startswith("setting type:")
                    or dl.startswith("default value:")
                    or dl.startswith("acceptable value")):
                continue
            if dl.startswith("description:"):
                author_desc.append(d.split(":", 1)[1].strip())
            else:
                desc.append(d)
            continue
        if s.startswith("#"):
            low = s[1:].strip()
            ll = low.lower()
            if ll.startswith("setting type:"):
                stype = low.split(":", 1)[1].strip()
            elif ll.startswith("default value:"):
                sdef = low.split(":", 1)[1].strip()
            elif ll.startswith("acceptable value range:"):
                srange = _cfg_range(low.split(":", 1)[1])
            elif ll.startswith("acceptable values:"):
                schoices = [x.strip() for x in low.split(":", 1)[1].split(",") if x.strip()]
            continue
        if "=" in s:
            k, _, v = s.partition("=")
            key = k.strip()
            val = v.strip()
            if not key:
                continue
            ptype = _classify_cfg_type(stype, val, schoices)
            out.append({
                "key": key,
                "label": key,
                "type": ptype,
                "default": sdef,
                "range": srange,
                "choices": schoices,
                "desc": " ".join(x for x in author_desc if x) or " ".join(x for x in desc if x),
                "section": section,
                "current": val,
            })
            desc, author_desc, stype, sdef, srange, schoices = [], [], "", "", None, None
    return out


def _is_lang_only_key(key):
    """历史遗留：早期隐藏 debug/日志/语言类参数。按用户要求已取消限制——
    cfg 中任何可设置项（布尔/数字/枚举/字符串等）一律列入，故恒为 False。"""
    return False


def _norm_cfg_name(s):
    """归一化为可比较键：仅保留字母数字，转小写。
    'Cafe Large Orders' -> 'cafelargeorders'，与 dll 基名对齐。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _cfg_aliases(cfg_path, fn):
    """为一个 cfg 生成全部关联别名：插件显示名（去空格）、GUID 全名、GUID 尾段、文件名。
    N 网 mod 的 GUID 尾段常与 dll 名不同（night.cafe.largeorders vs CafeLargeOrders），
    但 cfg 首行的 'created by plugin <NAME>' 就是 dll 基名，用它做主关联键。"""
    aliases = set()
    base = os.path.splitext(fn)[0]
    aliases.add(_norm_cfg_name(base))
    if "." in base:
        aliases.add(_norm_cfg_name(base.rsplit(".", 1)[-1]))
    try:
        with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
            head = f.read(4096)
        m = re.search(r"created by plugin\s+(.+?)\s+v?\d+\.\d+", head, re.I)
        if not m:
            m = re.search(r"created by plugin\s+(.+)", head, re.I)
        if m:
            name = m.group(1).strip()
            aliases.add(_norm_cfg_name(name))
    except Exception:
        pass
    aliases.discard("")
    return aliases


def fmt_size(n):
    """将字节数格式化为人类可读大小。"""
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def norm_join(base, rel):
    if not rel:
        return base
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    return os.path.normpath(os.path.join(base, rel))


# ---------- 一次性迁移：MachineLock 首次安装时清理旧版残留 ----------
MIG_MLOCK_LEGACY = "machinelock_legacy_cleanup"

# 旧版（v1.0 之前）CoffeeMachineLockToPlayer 的残留：配置 + 旧插件 dll
# (相对游戏目录, 目标文件名小写) —— 大小写不敏感匹配，兼容不同系统/命名习惯
LEGACY_MACHINE_LOCK_FILES = (
    ("BepInEx/config", "com.chiyodacoffee.coffeemachinelocktoplayer.cfg"),
    ("BepInEx/plugins", "coffeemachinelocktoplayer.dll"),
)


def migration_done(key):
    """一次性迁移是否已执行过。
    独立文件保存：_save_cfg() 每次会整份重写 config.json，放那里会被冲掉。"""
    flags = load_json("migrations.json", {}) or {}
    return flags.get(key) is True


def mark_migration_done(key):
    flags = load_json("migrations.json", {}) or {}
    flags[key] = True
    save_json("migrations.json", flags)


def legacy_machine_lock_files(game_dir):
    """返回游戏目录里仍存在的旧版残留文件绝对路径列表。"""
    found = []
    for rel, target in LEGACY_MACHINE_LOCK_FILES:
        folder = norm_join(game_dir, rel)
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except Exception as e:
            log_exc(e)
            continue
        for name in names:
            if name.lower() == target:
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    found.append(path)
    return found


def cleanup_legacy_machine_lock(game_dir):
    """删除旧版 CoffeeMachineLockToPlayer 残留，返回实际删除数量。
    按文件名精确匹配、不触碰其它模组；任何失败都被吞掉，绝不影响新模组安装。"""
    removed = 0
    for path in legacy_machine_lock_files(game_dir):
        try:
            os.remove(path)
            removed += 1
        except Exception as e:
            log_exc(e)
    return removed


def dir_has_dll(path):
    """目录（含一层子目录）里是否存在 dll —— 用于识别「目录型」模组。"""
    try:
        for fn in os.listdir(path):
            if fn.lower().endswith(".dll"):
                return True
        for fn in os.listdir(path):
            sub = os.path.join(path, fn)
            if os.path.isdir(sub):
                for s in os.listdir(sub):
                    if s.lower().endswith(".dll"):
                        return True
    except Exception:
        pass
    return False


def _dll_entries(path):
    """识别 plugins 下一个条目（文件或目录）的 dll 及启用状态。
    返回 (主 dll 名含 .dll, [(真实路径, 是否已禁用), ...])；不是 mod 返回 None。
    启用 = 后缀 .dll（BepInEx 加载）；禁用 = 后缀 .dll.disabled（BepInEx 忽略）。"""
    if os.path.isfile(path):
        fn = os.path.basename(path)
        low = fn.lower()
        if low.endswith(".dll.disabled"):
            return fn[:-len(".disabled")], [(path, True)]
        if low.endswith(".dll"):
            return fn, [(path, False)]
        return None
    if os.path.isdir(path):
        entries = []
        for root, _, files in os.walk(path):
            for f in files:
                lf = f.lower()
                if lf.endswith(".dll.disabled"):
                    entries.append((os.path.join(root, f), True))
                elif lf.endswith(".dll"):
                    entries.append((os.path.join(root, f), False))
        if not entries:
            return None
        main = next((os.path.basename(p) for p, dis in entries if not dis), None)
        if main is None:
            main = os.path.basename(entries[0][0])[:-len(".disabled")]
        return main, entries
    return None


def inner_dll_name(path):
    """目录型模组：返回目录内（含一层子目录）第一个 dll 的文件名（原样大小写）；找不到返回 None。
    用于让中文命名的文件夹仍按真实 dll 名匹配 catalog / 版本 / 参数。"""
    try:
        for fn in os.listdir(path):
            if fn.lower().endswith(".dll"):
                return fn
        for fn in os.listdir(path):
            sub = os.path.join(path, fn)
            if os.path.isdir(sub):
                for s in os.listdir(sub):
                    if s.lower().endswith(".dll"):
                        return s
    except Exception:
        pass
    return None


def file_sig(path):
    """返回 (大小/累计大小, 最新 mtime)。目录型模组递归统计全部文件。"""
    try:
        if os.path.isdir(path):
            total, latest = 0, 0
            for root, _, files in os.walk(path):
                for fn in files:
                    try:
                        st = os.stat(os.path.join(root, fn))
                    except Exception:
                        continue
                    total += st.st_size
                    if int(st.st_mtime) > latest:
                        latest = int(st.st_mtime)
            return (total, latest)
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime))
    except Exception:
        return None


class ToolTip:
    def __init__(self, widget):
        self.widget = widget
        self.tip = None

    def show(self, text, x, y, wraplength=430):
        self.hide()
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        lb = tk.Label(self.tip, text=text, justify="left", background="#fffff2",
                      relief="solid", borderwidth=1, wraplength=wraplength,
                      font=("Microsoft YaHei UI", 9), padx=4, pady=3)
        lb.pack()
        # 防 Tooltip 超出屏幕右侧/底部
        self.tip.update_idletasks()
        try:
            sw = self.tip.winfo_screenwidth()
            sh = self.tip.winfo_screenheight()
            tw = self.tip.winfo_reqwidth()
            th = self.tip.winfo_reqheight()
            px = min(x + 14, sw - tw - 10)
            py = min(y + 16, sh - th - 10)
            if px < 0:
                px = 0
            if py < 0:
                py = 0
            self.tip.wm_geometry("+%d+%d" % (px, py))
        except Exception:
            pass

    def hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollFrame(ttk.Frame):
    """可滚动的容器"""
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg="#ffffff", highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_wheel(self, event):
        if self.canvas.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def destroy(self):
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        super().destroy()


class Switch(tk.Canvas):
    """布尔滑块控件：true 开启 / false 关闭（文字可随界面语言切换）"""
    def __init__(self, master, value=False, command=None, text_on="On", text_off="Off", **kw):
        kw.setdefault("bg", "#ffffff")
        super().__init__(master, width=48, height=26, highlightthickness=0, bd=0, **kw)
        self.state = bool(value)
        self.command = command
        self._ton = text_on
        self._toff = text_off
        self.bind("<Button-1>", lambda e: self.toggle())
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = 48, 26
        if self.state:
            self.create_oval(0, 0, h, h, fill="#34c759", outline="")
            self.create_oval(w - h, 0, w, h, fill="#34c759", outline="")
            self.create_rectangle(h / 2, 0, w - h / 2, h, fill="#34c759", outline="")
            self.create_oval(w - h + 2, 2, w - 2, h - 2, fill="#ffffff", outline="")
        else:
            self.create_oval(0, 0, h, h, fill="#9e9e9e", outline="")
            self.create_oval(w - h, 0, w, h, fill="#9e9e9e", outline="")
            self.create_rectangle(h / 2, 0, w - h / 2, h, fill="#9e9e9e", outline="")
            self.create_oval(2, 2, h - 2, h - 2, fill="#ffffff", outline="")
        # 文字
        self.create_text(w / 2, h / 2 + 1, text=self._ton if self.state else self._toff,
                         font=("Microsoft YaHei UI", 8), fill="#ffffff")

    def toggle(self):
        self.state = not self.state
        self._draw()
        if self.command:
            self.command()

    def get(self):
        return self.state

    def set(self, v):
        self.state = bool(v)
        self._draw()
        if self.command:
            self.command()


class DotCheck(tk.Canvas):
    """极简圆形勾选框：未选=白底灰描边，选中=蓝底白勾。"""
    def __init__(self, master, value=False, command=None, size=22, **kw):
        bg = kw.pop("bg", "#ffffff")
        super().__init__(master, width=size, height=size, highlightthickness=0, bd=0, bg=bg, cursor="hand2", **kw)
        self.size = size
        self.state = bool(value)
        self.command = command
        self.bind("<Button-1>", lambda e: self._click())
        self._draw()

    def _click(self):
        self.state = not self.state
        self._draw()
        if self.command:
            self.command(self.state)

    def _draw(self):
        self.delete("all")
        n = self.size
        if self.state:
            self.create_oval(2, 2, n - 2, n - 2, fill="#2f6fed", outline="")
            self.create_line(n * 0.28, n * 0.52, n * 0.44, n * 0.68, n * 0.74, n * 0.34,
                             fill="#ffffff", width=2, capstyle="round", joinstyle="round")
        else:
            self.create_oval(2, 2, n - 2, n - 2, fill="#ffffff", outline="#c9ccd1", width=1.5)

    def get(self):
        return self.state

    def set(self, v):
        self.state = bool(v)
        self._draw()


def apply_style(root):
    """整体 UI：白色极简、干净线条（Marvis 风格）。"""
    root.configure(bg="#ffffff")
    try:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
    except Exception:
        return
    try:
        base = ("Microsoft YaHei UI", 10)
        st.configure(".", background="#ffffff", foreground="#1f2328", font=base)
        st.configure("TFrame", background="#ffffff")
        st.configure("TLabel", background="#ffffff", foreground="#1f2328")
        st.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"), foreground="#1f2328")
        st.configure("Sub.TLabel", foreground="#6e7781")
        st.configure("Muted.TLabel", foreground="#6e7781")
        st.configure("Faint.TLabel", foreground="#aab1b8")
        st.configure("Side.TFrame", background="#f6f7f9")
        st.configure("SideTitle.TLabel", background="#f6f7f9", foreground="#1f2328",
                     font=("Microsoft YaHei UI", 11, "bold"))
        # 按钮：白色 / 细线 / 主操作实心蓝
        st.configure("TButton", background="#ffffff", foreground="#1f2328",
                     bordercolor="#d5d9de", lightcolor="#ffffff", darkcolor="#d5d9de",
                     borderwidth=1, relief="flat", focusthickness=0, padding=(10, 5))
        st.map("TButton", background=[("active", "#f3f4f6"), ("pressed", "#e8eaed")],
               bordercolor=[("active", "#b9bec6")])
        st.configure("Tool.TButton", background="#ffffff", foreground="#1f2328",
                     bordercolor="#b9c0c9", lightcolor="#b9c0c9", darkcolor="#b9c0c9",
                     borderwidth=2, relief="flat", focusthickness=0, padding=(12, 6))
        st.map("Tool.TButton", background=[("active", "#f3f4f6"), ("pressed", "#e8eaed")],
               bordercolor=[("active", "#b9bec6")])
        # 次要按钮：灰底，悬停变实心蓝（右上角工具栏 / 弹窗次要操作共用）
        st.configure("Soft.TButton", background="#e8eaed", foreground="#1f2328",
                     bordercolor="#c9cdd3", lightcolor="#c9cdd3", darkcolor="#c9cdd3",
                     borderwidth=1, relief="flat", focusthickness=0, padding=(12, 6))
        st.map("Soft.TButton",
               background=[("disabled", "#e8eaed"), ("pressed", "#1f5fd6"), ("active", "#2f6fed")],
               foreground=[("disabled", "#9aa1a9"), ("pressed", "#ffffff"), ("active", "#ffffff")],
               bordercolor=[("pressed", "#1f5fd6"), ("active", "#2457c9")])
        st.configure("Primary.TButton", background="#2f6fed", foreground="#ffffff",
                     bordercolor="#2457c9", lightcolor="#2457c9", darkcolor="#2457c9",
                     borderwidth=2, relief="flat", focusthickness=0, padding=(16, 6))
        st.map("Primary.TButton", background=[("active", "#1f5fd6"), ("pressed", "#194fb0")])
        st.configure("Danger.TButton", background="#ffffff", foreground="#d1242f",
                     bordercolor="#e8b6b9", lightcolor="#e8b6b9", darkcolor="#e8b6b9",
                     borderwidth=1, relief="flat", focusthickness=0, padding=(12, 6))
        st.map("Danger.TButton", background=[("active", "#fdf0f0"), ("pressed", "#fbdcdc")],
               bordercolor=[("active", "#e6a0a5")])
        st.configure("Browse.TButton", background="#e8eaed", foreground="#1f2328",
                     bordercolor="#c9cdd3", lightcolor="#c9cdd3", darkcolor="#c9cdd3",
                     borderwidth=1, relief="flat", focusthickness=0, padding=(14, 6))
        st.map("Browse.TButton", background=[("active", "#d5d9de"), ("pressed", "#c9cdd3")],
               bordercolor=[("active", "#b9bec6")])
        st.configure("Param.TButton", background="#ffffff", foreground="#2f6fed",
                     bordercolor="#c7d7fb", lightcolor="#c7d7fb", darkcolor="#c7d7fb",
                     borderwidth=1, relief="flat", focusthickness=0, padding=(12, 5))
        st.map("Param.TButton", background=[("active", "#f0f5ff"), ("pressed", "#dde8ff")],
               bordercolor=[("active", "#9db9f7")])
        st.configure("TScrollbar", background="#e2e4e8", troughcolor="#ffffff",
                     arrowcolor="#8b949e", bordercolor="#ffffff", relief="flat")
        st.map("TScrollbar", background=[("active", "#c9cdd3")])
        st.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                     bordercolor="#d5d9de", lightcolor="#ffffff", darkcolor="#d5d9de",
                     arrowcolor="#57606a", padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", "#ffffff")])
        st.configure("TEntry", fieldbackground="#ffffff", bordercolor="#d5d9de",
                     lightcolor="#ffffff", darkcolor="#d5d9de", padding=3)
        st.configure("TLabelframe", bordercolor="#e5e7eb", lightcolor="#ffffff", darkcolor="#e5e7eb")
        st.configure("TLabelframe.Label", background="#ffffff", foreground="#1f2328")
    except Exception as e:
        log_exc(e)


class ModManagerApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        # 分辨率适配：参考设计尺寸 1380x800，小屏自动收缩进屏幕
        # （固定尺寸会把右侧按钮/底部状态挤出可视区域，客户小屏下看不到按钮）
        try:
            _sw = root.winfo_screenwidth()
            _sh = root.winfo_screenheight()
        except Exception:
            _sw, _sh = 1920, 1080
        self._des_w = 1380
        self._des_h = 800
        # 最小尺寸同样随屏收缩，防止小屏下窗口无法完整显示
        self._min_w = min(1120, _sw - 32) if _sw > 760 else 760
        self._min_h = min(640, _sh - 48) if _sh > 560 else 560
        _w = max(self._min_w, min(self._des_w, _sw - 24))
        _h = max(self._min_h, min(self._des_h, _sh - 72))
        # 无边框窗口 WM 不负责定位，默认居中显示
        try:
            _cx = max(0, (_sw - _w) // 2)
            _cy = max(0, (_sh - _h) // 2 - 20)
            root.geometry(f"{_w}x{_h}+{_cx}+{_cy}")
        except Exception:
            pass
        root.minsize(self._min_w, self._min_h)
        # 允许用户拖拽窗口边缘自由调整大小（无边框窗口需自绘边缘 resize）
        root.resizable(True, True)
        self._resize_dir = (0, 0)   # 当前拖拽方向: (mx, my)，mx/my ∈ {-1,0,1}
        self._resize_geo = None     # (x0, y0, w0, h0, offx, offy)
        apply_style(root)

        # 圆角窗体参数（透明色像素会变为透明，露出圆角白底外的部分）
        self.SHELL_TRANSPARENT = "#ff00f0"
        self._shell_ins = 46
        self._shell_rad = 40
        self._drag_off = None

        cfg = load_json("config.json") or {}
        # 首次启动默认英文：发布对象以国际用户为主，中文可在设置里切换
        self.lang = cfg.get("lang") or "en"
        if self.lang not in SUPPORTED_LANGS:
            self.lang = "en"
        self.S = _lang_sheet(self.lang)
        self.game_dir = (cfg.get("game_dir") or "").strip()
        self.game_exe = cfg.get("game_exe") or "CafeBusinessManager.exe"
        # 通用版：不随包分发 Mod，只读扫描游戏目录 BepInEx/plugins 与 BepInEx/config
        # 未设置游戏目录时留空，不拼相对路径（否则会误扫管理器自身目录）
        if self.game_dir:
            self.plugins_dir = norm_join(self.game_dir, cfg.get("plugins_subdir", "BepInEx/plugins"))
            self.config_dir = norm_join(self.game_dir, "BepInEx/config")
        else:
            self.plugins_dir = ""
            self.config_dir = ""

        # 参数支持完全由真实 cfg 动态决定：cfg_map = {dll基名lower: cfg文件名}
        self.cfg_map = {}
        # paramable = {dll基名lower: 是否存在非 Language 的可调参数}
        self.paramable = {}
        self.cfg_schema_cache = {}   # cfg文件名 -> (mtime, schema)

        # 每行记录
        self.rows = []
        self.plugins_files = {}
        self.source_sigs = {}
        # mod 描述悬停 ToolTip（列表行 hover 完整中文描述）
        self._row_tip = ToolTip(self.root)

        self._build_ui()
        self._load_left_panel()

        self.root.after(200, self._bootstrap)

    # ---------- UI ----------
    def _round_rect(self, c, x1, y1, x2, y2, r, **kw):
        """Canvas 平滑圆角多边形。"""
        pts = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
        return c.create_polygon(pts, smooth=True, splinesteps=24, **kw)

    def _resize_shell(self, e=None):
        """窗口尺寸变化时重绘圆角白底并同步内容区大小。"""
        c = self.shell
        w = e.width if e is not None else c.winfo_width()
        h = e.height if e is not None else c.winfo_height()
        if w < 80 or h < 80:
            return
        c.delete("bg")
        r = self._shell_rad
        self._round_rect(c, 1, 1, w - 1, h - 1, r,
                         fill="#ffffff", outline="#e2e5ea", width=1, tags=("bg",))
        c.tag_lower("bg")
        c.itemconfigure(self._content_win,
                        width=max(200, w - 2 * self._shell_ins),
                        height=max(120, h - 2 * self._shell_ins))
        # 窗口尺寸变化后让描述文本按新列宽重新换行
        self.root.after_idle(self._refresh_row_wrap)

    def _desc_wrap(self):
        """mod 描述文本的可换行宽度：跟随列表区实际宽度，防止窄窗口下文本被横向切断。"""
        try:
            lw = self.list_canvas.winfo_width()
        except Exception:
            lw = 0
        if lw < 100:
            # 兜底估算：列表区约等于内容宽扣除左栏与参数列
            lw = self.root.winfo_width() - 2 * self._shell_ins - 340
        return max(200, lw - 210)

    def _refresh_row_wrap(self):
        try:
            lw = self.list_canvas.winfo_width()
        except Exception:
            return
        if lw < 100:
            return
        wrap = max(200, lw - 210)
        for info in getattr(self, "row_widgets", {}).values():
            lbd = info.get("desc")
            if lbd is not None and lbd.winfo_exists():
                lbd.configure(wraplength=wrap)

    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        off = self._drag_off
        if off:
            self.root.geometry(f"+{e.x_root - off[0]}+{e.y_root - off[1]}")

    # ---------- 边缘缩放（无边框窗口无原生手柄，自绘边缘拖拽） ----------
    EDGE = 7  # 触发缩放的边缘像素宽度

    def _edge_dir(self, e):
        """返回鼠标相对窗口边缘的方向 (mx, my)：-1 左/上，1 右/下，0 内部。"""
        try:
            x = e.x_root - self.root.winfo_rootx()
            y = e.y_root - self.root.winfo_rooty()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
        except Exception:
            return (0, 0)
        mx = -1 if x < self.EDGE else (1 if x >= w - self.EDGE else 0)
        my = -1 if y < self.EDGE else (1 if y >= h - self.EDGE else 0)
        return (mx, my)

    def _edge_cursor(self, mx, my):
        if mx == -1 and my == -1:
            return "size_nw_se"
        if mx == 1 and my == 1:
            return "size_nw_se"
        if mx == -1 and my == 1:
            return "size_ne_sw"
        if mx == 1 and my == -1:
            return "size_ne_sw"
        if mx == -1 or mx == 1:
            return "size_we"
        if my == -1 or my == 1:
            return "size_ns"
        return ""

    def _on_edge_motion(self, e):
        """鼠标悬停在窗口边缘时切换缩放光标；落在内部控件上不显示缩放光标。"""
        if e.widget not in (self.root, getattr(self, "shell", None)):
            return
        mx, my = self._edge_dir(e)
        cur = self._edge_cursor(mx, my)
        if cur != "":
            self.root.config(cursor=cur)
        else:
            self.root.config(cursor="")

    def _on_edge_press(self, e):
        # 仅窗口空白背景（根/外壳画布）允许边缘缩放；内部控件（滚动条、按钮等）
        # 上的按下不触发 resize，避免拖动滚动条时误缩放窗口导致界面抖动/重影
        if e.widget not in (self.root, getattr(self, "shell", None)):
            self._resize_dir = (0, 0)
            self._resize_geo = None
            return
        mx, my = self._edge_dir(e)
        if mx == 0 and my == 0:
            self._resize_dir = (0, 0)
            self._resize_geo = None
            return
        self._resize_dir = (mx, my)
        self._resize_geo = (self.root.winfo_x(), self.root.winfo_y(),
                            self.root.winfo_width(), self.root.winfo_height(),
                            e.x_root, e.y_root)

    def _on_edge_drag(self, e):
        d = self._resize_geo
        mx, my = self._resize_dir
        if not d or (mx == 0 and my == 0):
            return
        x0, y0, w0, h0, sx, sy = d
        dx = e.x_root - sx
        dy = e.y_root - sy
        nw, nh = w0, h0
        nx, ny = x0, y0
        if mx == -1:
            nx = min(x0 + dx, x0 + w0 - self._min_w)
            nw = x0 + w0 - nx
        elif mx == 1:
            nw = w0 + dx
        if my == -1:
            ny = min(y0 + dy, y0 + h0 - self._min_h)
            nh = y0 + h0 - ny
        elif my == 1:
            nh = h0 + dy
        # 夹紧到最小/最大尺寸（上限为屏幕，避免拖出可视范围难以收回）
        try:
            _mw = self.root.winfo_screenwidth()
            _mh = self.root.winfo_screenheight()
        except Exception:
            _mw, _mh = 1920, 1080
        nw = max(self._min_w, min(nw, _mw))
        nh = max(self._min_h, min(nh, _mh))
        if mx == -1:
            nx = min(nx, x0 + w0 - self._min_w)
        if my == -1:
            ny = min(ny, y0 + h0 - self._min_h)
        self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")

    def _on_edge_release(self, e):
        self._resize_dir = (0, 0)
        self._resize_geo = None

    def _build_ui(self):
        # 圆角窗体：Canvas 画白色圆角矩形，四周透明色镂空，内容嵌入内边距区域
        self.shell = tk.Canvas(self.root, highlightthickness=0, bd=0,
                               bg="#ffffff" if getattr(self.root, "_no_ts", False) else self.SHELL_TRANSPARENT)
        self.shell.pack(fill="both", expand=True)
        main = ttk.Frame(self.shell)
        self._content_win = self.shell.create_window(
            (self._shell_ins, self._shell_ins), window=main, anchor="nw", tags=("content",))
        self.shell.bind("<Configure>", self._resize_shell)

        # 顶部：标题 + 操作按钮（白色极简）
        top = ttk.Frame(main)
        top.pack(fill="x")
        title_box = ttk.Frame(top)
        title_box.pack(side="left")
        self.lbl_title = ttk.Label(title_box, text=self.S["title_mod"], style="Title.TLabel")
        self.lbl_title.pack(anchor="w")
        self.lbl_sub = ttk.Label(title_box, text=self.S["subtitle"], style="Sub.TLabel")
        self.lbl_sub.pack(anchor="w", pady=(2, 0))
        self.lbl_bep = ttk.Label(title_box, text=self.S["bep_checking"], style="Muted.TLabel")
        self.lbl_bep.pack(anchor="w", pady=(1, 0))
        # 拖动窗口：标题文字与顶栏空白处均可拖（按钮、✕ 除外）
        for w in (title_box, self.lbl_title, self.lbl_sub, self.lbl_bep):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        top.bind("<Button-1>", self._drag_start)
        top.bind("<B1-Motion>", self._drag_move)
        btn_box = ttk.Frame(top)
        btn_box.pack(side="right")
        btn_box.bind("<Button-1>", self._drag_start)
        btn_box.bind("<B1-Motion>", self._drag_move)
        self.btn_open_dir = ttk.Button(btn_box, text=self.S["btn_open_game_dir"], style="Soft.TButton",
                                       command=lambda: self._open_dir(self.game_dir))
        self.btn_open_dir.pack(side="left", padx=(0, 8))
        self.btn_bep = ttk.Button(btn_box, text=self.S["btn_install_bep"], style="Soft.TButton",
                                  command=self.install_bepinex)
        self.btn_bep.pack(side="left", padx=(0, 8))
        self.btn_settings = ttk.Button(btn_box, text=self.S["btn_settings"], style="Soft.TButton",
                                       command=self.open_settings)
        self.btn_settings.pack(side="left", padx=(0, 8))
        self.lbl_close = tk.Label(btn_box, text="✕", font=("Segoe UI Symbol", 12, "bold"),
                                  fg="#9aa1a9", bg="#ffffff", padx=6, pady=0, cursor="hand2")
        self.lbl_close.pack(side="left", padx=(14, 0))
        self.lbl_close.bind("<Enter>", lambda e: self.lbl_close.config(fg="#d1242f"))
        self.lbl_close.bind("<Leave>", lambda e: self.lbl_close.config(fg="#9aa1a9"))
        self.lbl_close.bind("<Button-1>", lambda e: self.root.destroy())

        # 主体：左侧使用说明 + 右侧 mod 列表
        body = ttk.Frame(main)
        body.pack(fill="both", expand=True, pady=(14, 0))

        left = ttk.Frame(body, width=320, style="Side.TFrame")
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.txt_panel = tk.Text(left, wrap="char", font=("Microsoft YaHei UI", 9),
                                 bg="#f6f7f9", fg="#57606a", relief="flat",
                                 highlightthickness=0, borderwidth=0, cursor="arrow",
                                 padx=14, pady=4)
        self.txt_panel.pack(fill="both", expand=True, pady=(14, 10))
        # 富文本排版样式（内容在 _load_left_panel 中按行渲染）
        # 左对齐；标题不缩进，正文统一缩进，子项（列表/箭头/括号说明）进一步缩进
        self.txt_panel.tag_configure("t_title", font=("Microsoft YaHei UI", 10, "bold"),
                                     foreground="#1f2328", spacing1=12, spacing3=6,
                                     lmargin1=0, lmargin2=0)
        # 首行标题：去掉上方间距，直接占据原「使用说明」位置
        self.txt_panel.tag_configure("t_title_top", font=("Microsoft YaHei UI", 10, "bold"),
                                     foreground="#1f2328", spacing1=0, spacing3=6,
                                     lmargin1=0, lmargin2=0)
        self.txt_panel.tag_configure("t_text", font=("Microsoft YaHei UI", 9),
                                     foreground="#57606a", spacing1=2, spacing3=4,
                                     lmargin1=0, lmargin2=0)
        self.txt_panel.tag_configure("t_num", font=("Microsoft YaHei UI", 9),
                                     foreground="#57606a", spacing1=3, spacing3=5,
                                     lmargin1=0, lmargin2=0)
        self.txt_panel.tag_configure("t_sub", font=("Microsoft YaHei UI", 9),
                                     foreground="#57606a", spacing1=2, spacing3=4,
                                     lmargin1=22, lmargin2=22)
        self.txt_panel.tag_configure("t_link", foreground="#2f6fed", underline=1)
        # 底部版本提示：加粗 + 与上方文字间隔一行（spacing1 提供上方留白）
        self.txt_panel.tag_configure("t_note", font=("Microsoft YaHei UI", 9, "bold"),
                                     foreground="#1f2328", spacing1=12, spacing3=4,
                                     lmargin1=0, lmargin2=0)
        self.txt_panel.tag_bind("t_link", "<Button-1>",
                                lambda e: webbrowser.open("https://github.com/BepInEx/BepInEx/releases"))
        self.txt_panel.bind("<Motion>", self._on_usage_motion)
        self.txt_panel.bind("<MouseWheel>", self._on_usage_wheel)
        self.txt_panel.configure(state="disabled")

        tk.Frame(body, bg="#e8eaed", width=1).pack(side="left", fill="y")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))

        # 列表工具栏：左侧提示 + 右侧「保存」按钮（保存勾选的启用/停用变更）
        list_bar = ttk.Frame(right)
        list_bar.pack(fill="x", pady=(0, 6))
        self.lbl_sel = ttk.Label(list_bar, text=self.S["sel_hint"], style="Muted.TLabel")
        self.lbl_sel.pack(side="left")
        self.btn_apply = ttk.Button(list_bar, text=self.S["btn_save"], style="Primary.TButton",
                                    command=self.on_save)
        self.btn_apply.pack(side="right")

        # 列表滚动区（自绘行：参数列使用真实按钮）
        list_host = ttk.Frame(right)
        list_host.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(list_host, bg="#ffffff", highlightthickness=0, bd=0)
        self._scrolling = False
        self._scroll_end_after = None
        self._yview_pending = None
        self._yview_after = None
        self._scrollregion_after = None

        def _on_scroll_begin(e=None):
            self._scrolling = True
            if self._scroll_end_after:
                try:
                    self.root.after_cancel(self._scroll_end_after)
                except Exception:
                    pass
                self._scroll_end_after = None
            self._row_tip.hide()

        def _on_scroll_end(e=None):
            self._scroll_end_after = self.root.after(180, self._end_scrolling)

        vsb = ttk.Scrollbar(list_host, orient="vertical", command=self._yview_throttled)
        vsb.bind("<ButtonPress-1>", _on_scroll_begin)
        vsb.bind("<ButtonRelease-1>", _on_scroll_end)
        self.list_canvas.configure(yscrollcommand=vsb.set)
        # 整数像素滚动：消除亚像素位移导致的文字模糊/重影，滚动更顺滑
        self.list_canvas.configure(yscrollincrement=1, xscrollincrement=1)
        vsb.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_inner = tk.Frame(self.list_canvas, bg="#ffffff")
        self._list_win = self.list_canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind("<Configure>", self._queue_scrollregion)
        self.list_canvas.bind("<Configure>", lambda e: self.list_canvas.itemconfigure(self._list_win, width=e.width))
        self._lc_hover = False
        self.list_canvas.bind("<Enter>", lambda e: setattr(self, "_lc_hover", True))
        self.list_canvas.bind("<Leave>", lambda e: setattr(self, "_lc_hover", False))
        self.list_canvas.bind("<MouseWheel>", self._on_list_wheel)
        self.list_canvas.bind("<Button-4>", lambda e: self._on_list_wheel_delta(e, 1))
        self.list_canvas.bind("<Button-5>", lambda e: self._on_list_wheel_delta(e, -1))
        # 滚轮修复：mod 行是 Canvas 内嵌 Frame/Label 子控件，鼠标停在行上滚动时事件目标不是
        # list_canvas，原绑定不会触发且会落入左侧 ScrollFrame 的 bind_all（滚动的是左面板）。
        # 因此在 root 层追加坐标判定：鼠标位于列表区域内的滚轮事件一律滚动 mod 清单并截断冒泡。
        self.root.bind("<MouseWheel>", self._on_list_area_wheel, add="+")
        self.root.bind("<Button-4>", lambda e: self._on_list_area_wheel_btn(e, 1), add="+")
        self.root.bind("<Button-5>", lambda e: self._on_list_area_wheel_btn(e, -1), add="+")

        # 边缘缩放：事件挂到 root 层，鼠标在窗口最外缘按下/拖动时调整尺寸
        self.root.bind("<Motion>", self._on_edge_motion, add="+")
        self.root.bind("<Button-1>", self._on_edge_press, add="+")
        self.root.bind("<B1-Motion>", self._on_edge_drag, add="+")
        self.root.bind("<ButtonRelease-1>", self._on_edge_release, add="+")

        # 底部状态行：左侧状态文本 + 右下角作者水印（版权署名，防倒卖标记）
        status_row = ttk.Frame(right)
        status_row.pack(fill="x", pady=(8, 0))
        self.status = ttk.Label(status_row, text=self.S["status_ready"], style="Muted.TLabel", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.watermark = tk.Label(status_row, text="©LemonK",
                                  font=("Microsoft YaHei UI", 8),
                                  fg="#c3c9d0", bg="#ffffff", cursor="arrow")
        self.watermark.pack(side="right", padx=(14, 0))

        # BepInEx 安装进度（默认隐藏，安装时显示）
        self.prog_lbl = ttk.Label(right, text="", style="Muted.TLabel", anchor="w")
        self.prog_bar = ttk.Progressbar(right, orient="horizontal", length=320, mode="determinate")
        self._prog_visible = False

        self.pending = {}      # file -> 'install'|'remove'
        self.selected = []     # 点击行选中（删除所选的作用对象）
        self.row_widgets = {}  # file -> {"accent":…, "badge":…}

    # ---------- 列表滚动流畅度 ----------
    def _yview_throttled(self, *args):
        """滑块拖动时 yview 调用极密集，合并到每帧一次（约 16ms），避免 Canvas
        内嵌的每行 widget 被反复重绘造成卡顿/文字重影。"""
        self._scrolling = True
        self._yview_pending = args
        if self._yview_after is None:
            self._yview_after = self.root.after(16, self._flush_yview)

    def _flush_yview(self):
        self._yview_after = None
        if self._yview_pending is not None:
            args = self._yview_pending
            self._yview_pending = None
            self.list_canvas.yview(*args)

    def _queue_scrollregion(self, e=None):
        """行内容变化时重算 scrollregion；用 after_idle 合并同一批布局事件。"""
        if self._scrollregion_after is not None:
            return
        self._scrollregion_after = self.root.after_idle(self._flush_scrollregion)

    def _flush_scrollregion(self):
        self._scrollregion_after = None
        try:
            self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))
        except Exception:
            pass

    def _end_scrolling(self):
        self._scroll_end_after = None
        self._scrolling = False

    # ---------- 列表行交互 ----------
    @staticmethod
    def _wheel_units(delta):
        """鼠标滚轮 delta → 滚动步长。Canvas 默认 1px/unit，原代码一格仅滚 1px 极不跟手；
        这里放大到 50px/格，并对触控板细分 delta 也做响应，避免滚动失灵。"""
        mag = max(1, abs(delta) // 120) * 50
        return -mag if delta > 0 else mag

    def _mark_wheel_scroll(self):
        self._scrolling = True
        self._row_tip.hide()
        if self._scroll_end_after:
            try:
                self.root.after_cancel(self._scroll_end_after)
            except Exception:
                pass
        self._scroll_end_after = self.root.after(180, self._end_scrolling)

    def _on_list_wheel(self, e):
        if self._lc_hover:
            self._mark_wheel_scroll()
            self.list_canvas.yview_scroll(self._wheel_units(e.delta), "units")
        return "break"

    def _on_list_wheel_delta(self, e, units):
        if self._lc_hover:
            self._mark_wheel_scroll()
            self.list_canvas.yview_scroll(-units * 50, "units")
        return "break"

    def _in_list_area(self, e):
        """判断鼠标是否位于右侧 mod 列表显示区域内（行子控件滚动时事件目标不是 list_canvas）。"""
        try:
            lc = self.list_canvas
            if not lc.winfo_exists():
                return False
            return (lc.winfo_rootx() <= e.x_root <= lc.winfo_rootx() + lc.winfo_width()
                    and lc.winfo_rooty() <= e.y_root <= lc.winfo_rooty() + lc.winfo_height())
        except Exception:
            return False

    def _on_list_area_wheel(self, e):
        if self._in_list_area(e):
            self._mark_wheel_scroll()
            self.list_canvas.yview_scroll(self._wheel_units(e.delta), "units")
            return "break"

    def _on_list_area_wheel_btn(self, e, units):
        if self._in_list_area(e):
            self._mark_wheel_scroll()
            self.list_canvas.yview_scroll(-units * 50, "units")
            return "break"

    def _bind_row_select(self, w, file_):
        w.bind("<Button-1>", lambda e, ff=file_: self._toggle_select(ff))

    def _bind_row_tip(self, w, text):
        """整行绑定悬停 ToolTip：延迟 260ms 显示，离开后延迟 140ms 再隐藏（行内移动不闪烁）。"""
        def _enter(e):
            if getattr(self, "_scrolling", False):
                return
            if getattr(self, "_tip_hide_after", None):
                try:
                    self.root.after_cancel(self._tip_hide_after)
                except Exception:
                    pass
                self._tip_hide_after = None
            self._tip_after = self.root.after(
                260, lambda: self._row_tip.show(text, e.x_root, e.y_root))

        def _cancel(e=None):
            if getattr(self, "_scrolling", False):
                return
            if getattr(self, "_tip_after", None):
                try:
                    self.root.after_cancel(self._tip_after)
                except Exception:
                    pass
                self._tip_after = None
            self._tip_hide_after = self.root.after(140, self._row_tip.hide)

        def _click(e):
            if getattr(self, "_tip_after", None):
                try:
                    self.root.after_cancel(self._tip_after)
                except Exception:
                    pass
                self._tip_after = None
            if getattr(self, "_tip_hide_after", None):
                try:
                    self.root.after_cancel(self._tip_hide_after)
                except Exception:
                    pass
                self._tip_hide_after = None
            self._row_tip.hide()

        w.bind("<Enter>", _enter)
        w.bind("<Leave>", _cancel)
        w.bind("<Button-1>", _click, add="+")

    def _toggle_select(self, file_):
        if file_ in self.selected:
            self.selected.remove(file_)
            on = False
        else:
            self.selected.append(file_)
            on = True
        info = self.row_widgets.get(file_)
        if info and info.get("accent"):
            info["accent"].configure(bg="#2f6fed" if on else "#ffffff")
        self._sync_sel_ui()

    def _toggle_select_all(self):
        """全选/取消全选列表行（作用于行选中状态）。"""
        all_files = [r["file"] for r in self.rows]
        if all_files and len(self.selected) >= len(all_files):
            self.selected = []
        else:
            self.selected = list(all_files)
        for fn, info in self.row_widgets.items():
            if info and info.get("accent"):
                info["accent"].configure(bg="#2f6fed" if fn in self.selected else "#ffffff")
        self._sync_sel_ui()

    def _sync_sel_ui(self):
        """通用版：工具栏固定显示勾选/保存提示（无全选/删除功能）。"""
        if hasattr(self, "lbl_sel"):
            self.lbl_sel.config(text=self.S["sel_hint"])

    def _flip_check(self, file_, val):
        r = next((x for x in self.rows if x["file"] == file_), None)
        if r is None:
            return
        r["enabled"] = bool(val)
        action = "enable" if val else "disable"
        # 与原始状态相同则撤销 pending，避免无谓保存
        if bool(val) == (r["status"] == "enabled"):
            self.pending.pop(file_, None)
        else:
            self.pending[file_] = action
        self._update_row_badge(file_)
        self._refresh_check_label()

    def _badge_for(self, r):
        fn = r["file"]
        if self.pending.get(fn) == "enable":
            return self.S["badge_to_enable"], "#2f6fed"
        if self.pending.get(fn) == "disable":
            return self.S["badge_to_disable"], "#d97706"
        return "", ""

    def _update_row_badge(self, file_):
        info = self.row_widgets.get(file_)
        if not info:
            return
        old = info.get("badge")
        if old is not None:
            old.destroy()
            info["badge"] = None
        r = next((x for x in self.rows if x["file"] == file_), None)
        if not r:
            return
        txt, color = self._badge_for(r)
        if txt:
            info["badge"] = self._add_badge(info["top"], txt, color)

    def _add_badge(self, parent, txt, color):
        lb = tk.Label(parent, text=txt, bg="#ffffff", fg=color,
                      font=("Microsoft YaHei UI", 8), cursor="hand2")
        lb.pack(side="left", padx=(8, 0))
        self._bind_row_select(lb, parent._mod_file)
        return lb

    # ---------- 自绘列表行 ----------
    def _render_rows(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        self.row_widgets = {}
        self.selected = []
        if not self.rows:
            tk.Label(self.list_inner, text=self.S["empty_list"],
                     bg="#ffffff", fg="#9aa0a6", font=("Microsoft YaHei UI", 10)).pack(pady=36)
            return
        for r in self.rows:
            self._build_row(r)
            tk.Frame(self.list_inner, bg="#eef0f2", height=1).pack(fill="x")

    def _build_row(self, r):
        fn = r["file"]
        row = tk.Frame(self.list_inner, bg="#ffffff")
        row.pack(fill="x")
        row.grid_columnconfigure(2, weight=1)
        row.grid_columnconfigure(3, weight=0)

        accent = tk.Frame(row, bg="#ffffff", width=4)
        accent.grid(row=0, column=0, sticky="ns")

        # 启用勾选框：选中=启用(.dll)，取消=禁用(.dll.disabled)，保存后改名生效
        check = DotCheck(row, value=bool(r["enabled"]),
                         command=lambda v, ff=fn: self._flip_check(ff, v))
        check.grid(row=0, column=1, padx=(10, 12), pady=10, sticky="n")

        body = tk.Frame(row, bg="#ffffff")
        body.grid(row=0, column=2, sticky="ew", pady=8)
        body._mod_file = fn
        self._bind_row_select(body, fn)

        top = tk.Frame(body, bg="#ffffff")
        top.pack(fill="x")
        top._mod_file = fn
        self._bind_row_select(top, fn)

        disp_name = self._display_name(r)

        lb = tk.Label(top, text=disp_name, bg="#ffffff", fg="#1f2328",
                      font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2")
        lb.pack(side="left")
        self._bind_row_select(lb, fn)
        # 名称列按当前语言显示单一名称（不再拼接双语）

        txt, color = self._badge_for(r)
        if txt:
            bad = tk.Label(top, text=txt, bg="#ffffff", fg=color,
                           font=("Microsoft YaHei UI", 8), cursor="hand2")
            bad.pack(side="left", padx=(8, 0))
            self._bind_row_select(bad, fn)
        else:
            bad = None

        bot = tk.Frame(body, bg="#ffffff")
        bot.pack(fill="x", pady=(2, 0))
        bot._mod_file = fn
        self._bind_row_select(bot, fn)
        # 通用版无清单：第三方 mod 的 FileVersion 多为占位值，不显示以免误导
        lbm = tk.Label(bot, text=r.get("match_name") or fn, bg="#ffffff", fg="#8b949e",
                       font=("Microsoft YaHei UI", 9))
        lbm.pack(anchor="w")
        dll_key_down = _norm_cfg_name(os.path.splitext(r.get("match_name") or fn)[0])
        schema = []
        if getattr(self, "paramable", {}).get(dll_key_down):
            cfg_fn = self._cfg_for_dll(r.get("match_name") or fn)
            if cfg_fn:
                schema = [p for p in self._schema_for(cfg_fn) if not _is_lang_only_key(p.get("key"))]
        if schema:
            tip = self.S["universal_param_count"].format(n=len(schema))
            lbd = tk.Label(bot, text=tip, bg="#ffffff", fg="#57606a", justify="left", anchor="w",
                           font=("Microsoft YaHei UI", 9))
            lbd.pack(anchor="w", pady=(2, 0))

        # 参数按钮（仅真实存在可调参数的 mod；纯日志语言 Language 参数的 cfg 视为不支持）
        btn_area = tk.Frame(row, bg="#ffffff")
        btn_area.grid(row=0, column=3, sticky="e", padx=(6, 14))
        dll_key = _norm_cfg_name(os.path.splitext(r.get("match_name") or fn)[0])
        if getattr(self, "paramable", {}).get(dll_key):
            b = ttk.Button(btn_area, text=self.S["btn_param_edit"], style="Primary.TButton",
                           command=lambda rr=r: self.open_param_editor(rr))
            b.pack(anchor="center", pady=10)
        else:
            # 通用版：部分 mod 本身不生成 cfg，统一显示中性文案，不做橙色提示
            tk.Label(btn_area, text=self.S["param_na"], bg="#ffffff", fg="#c0c4cb",
                     font=("Microsoft YaHei UI", 9), wraplength=190,
                     justify="right").pack(anchor="center", pady=10)

        self.row_widgets.setdefault(fn, {}).update(
            {"accent": accent, "badge": bad, "top": top})

    def _open_dir(self, path):
        if path and os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showwarning(self.S["t_warn"], self.S["dir_not_found"].format(path=str(path)))

    def open_settings(self):
        w = tk.Toplevel(self.root)
        w.configure(bg="#ffffff")
        w.title(self.S["set_title"])
        w.geometry("760x400")
        w.minsize(680, 360)
        w.transient(self.root)
        w.grab_set()
        w.columnconfigure(1, weight=1)
        ttk.Label(w, text=self.S["set_lbl_game_dir"], style="Muted.TLabel").grid(row=1, column=0, sticky="e", padx=(16, 8), pady=6)
        sv_dir = tk.StringVar(value=self.game_dir)
        sv_exe = tk.StringVar(value=self.game_exe)
        e2 = ttk.Entry(w, textvariable=sv_dir)
        e2.grid(row=1, column=1, sticky="ew", padx=4, pady=6)
        ttk.Label(w, text=self.S["set_lbl_game_exe"], style="Muted.TLabel").grid(row=2, column=0, sticky="e", padx=(16, 8), pady=6)
        e3 = ttk.Entry(w, textvariable=sv_exe)
        e3.grid(row=2, column=1, sticky="ew", padx=4, pady=6)
        # 语言选择：持久化到 config.json 的 lang 字段，保存后主界面即时重建生效
        lang_var = tk.StringVar(value=LANG_NAMES.get(self.lang, LANG_NAMES["en"]))
        ttk.Label(w, text=self.S["set_lbl_lang"], style="Muted.TLabel").grid(
            row=3, column=0, sticky="e", padx=(16, 8), pady=6)
        lang_box = ttk.Combobox(w, textvariable=lang_var, values=list(LANG_NAMES.values()),
                                state="readonly", width=18)
        lang_box.grid(row=3, column=1, sticky="w", padx=4, pady=6)

        # 自动匹配标记：用户手动浏览选定 exe 后不再被目录变化覆盖
        auto_flag = {"on": not bool(sv_exe.get().strip())}

        def auto_exe_from_dir(*_):
            """游戏目录变化时，目录内存在 CafeBusinessManager.exe 则自动填入。"""
            gd = sv_dir.get().strip()
            if not gd or not os.path.isdir(gd):
                return
            name = ""
            if os.path.exists(os.path.join(gd, "CafeBusinessManager.exe")):
                name = "CafeBusinessManager.exe"
            else:
                exes = [f for f in os.listdir(gd) if f.lower().endswith(".exe")]
                if len(exes) == 1:
                    name = exes[0]
            if not name:
                return
            cur = sv_exe.get().strip()
            if auto_flag["on"] or (not cur) or (cur == "CafeBusinessManager.exe" and name != cur):
                sv_exe.set(name)
                auto_flag["on"] = True

        sv_dir.trace_add("write", auto_exe_from_dir)
        # 已保存过游戏目录但 exe 为空时，打开设置即自动补全
        try:
            auto_exe_from_dir()
        except Exception:
            pass

        def pick_dir(entry, var=None):
            # 先释放模态抓取，避免系统目录选择框无法弹出/无法交互
            try:
                w.grab_release()
            except Exception:
                pass
            try:
                d = filedialog.askdirectory(parent=w, title=self.S["dlg_choose_dir"])
                if d:
                    d = d.replace("/", "\\")
                    if var is not None:
                        var.set(d)
                    else:
                        entry.delete(0, "end")
                        entry.insert(0, d)
            except Exception as e:
                log_exc(e)
                messagebox.showwarning(self.S["select_fail"], str(e))
            finally:
                try:
                    w.grab_set()
                except Exception:
                    pass

        def pick_file(entry):
            try:
                w.grab_release()
            except Exception:
                pass
            try:
                f = filedialog.askopenfilename(parent=w, title=self.S["dlg_choose_exe"],
                                               filetypes=[(self.S["ft_exe"], "*.exe"), (self.S["ft_all"], "*.*")])
                if f:
                    f = f.replace("/", "\\")
                    auto_flag["on"] = False  # 手动指定后停止自动覆盖
                    entry.delete(0, "end")
                    entry.insert(0, f)
            except Exception as e:
                log_exc(e)
                messagebox.showwarning(self.S["select_fail"], str(e))
            finally:
                try:
                    w.grab_set()
                except Exception:
                    pass

        def ok():
            old_lang = self.lang
            new_lang = next((code for code, nm in LANG_NAMES.items() if nm == lang_var.get()), "en")
            if new_lang not in SUPPORTED_LANGS:
                new_lang = "en"
            self.game_dir = e2.get().strip()
            self.game_exe = e3.get().strip()
            if not self.game_exe:
                self.game_exe = "CafeBusinessManager.exe"
            self.plugins_dir = norm_join(self.game_dir, "BepInEx/plugins")
            self.config_dir = norm_join(self.game_dir, "BepInEx/config")
            # 语言切换：未保存变更保留，其余主界面即时以新语言重建生效（无需重启）
            lang_changed = (new_lang != old_lang)
            self.lang = new_lang
            self.S = _lang_sheet(new_lang)
            self._save_cfg()
            if lang_changed:
                self._apply_lang()
            self.refresh()
            # 用户设置完游戏目录后：检查注入器三件是否已在游戏根目录，缺失直接放入
            self._auto_ensure_doorstop()
            w.destroy()

        ttk.Button(w, text=self.S["browse"], style="Browse.TButton", command=lambda: pick_dir(e2, sv_dir)).grid(row=1, column=2, padx=(8, 16), pady=6)
        ttk.Button(w, text=self.S["browse"], style="Browse.TButton", command=lambda: pick_file(e3)).grid(row=2, column=2, padx=(8, 16), pady=6)
        ttk.Label(w, text=self.S["set_hint_auto"], style="Muted.TLabel").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=16, pady=(6, 0))
        ttk.Button(w, text=self.S["btn_save"], style="Primary.TButton", command=ok).grid(row=5, column=1, pady=14, sticky="e")
        w.bind("<Return>", lambda e: ok())

    # ---------- 语言即时应用 ----------
    def _apply_lang(self):
        """语言切换后：静态文案(标题/副标题/徽标说明/参数按钮)即时重建，未保存变更保留。"""
        self.lbl_title.config(text=self.S["title_mod"])
        self.lbl_sub.config(text=self.S["subtitle"])
        for btn, key in ((self.btn_open_dir, "btn_open_game_dir"),
                         (self.btn_bep, "btn_install_bep"),
                         (self.btn_settings, "btn_settings")):
            btn.config(text=self.S[key])
        self._refresh_bepinex_label()
        self.lbl_sel.config(text=self.S["sel_hint"])
        self.btn_apply.config(text=self.S["btn_save"])
        self._load_left_panel()
        self._render_rows()
        self._sync_sel_ui()

    def _save_cfg(self):
        cfg = {
            "game_dir": self.game_dir.replace("\\", "/"),
            "game_exe": self.game_exe,
            "plugins_subdir": "BepInEx/plugins",
            "lang": self.lang,
        }
        save_json("config.json", cfg)

    # ---------- 左侧使用说明 ----------
    def _load_left_panel(self):
        """左侧使用说明富文本渲染：标题加粗分段、正文/子项分级缩进、整体左对齐、URL 高亮可点。"""
        tp = self.txt_panel
        tp.configure(state="normal")
        tp.delete("1.0", "end")
        url = "https://github.com/BepInEx/BepInEx/releases"
        usage = USAGE_TEXT.get(self.lang) or USAGE_TEXT.get("en", "")
        first = True
        note_block = False
        for raw in usage.splitlines():
            if not raw.strip():
                continue
            body = raw.strip()
            # 标题：方括号 [xxx] 或全角括号 【xxx】（独占一行）
            is_head = ((body.startswith("[") and body.endswith("]")) or
                       (body.startswith("【") and body.endswith("】")))
            if is_head:
                tp.insert("end", body + "\n", "t_title_top" if first else "t_title")
                first = False
                note_block = False
                continue
            # 底部版本提示块（以「注意/Note」开头）：整段加粗，且与上方间隔一行
            if body.startswith(("注意", "Note")):
                note_block = True
            if note_block:
                tp.insert("end", body + "\n", "t_note")
                first = False
                continue
            # 编号条目（1. 2. …）使用悬挂缩进，换行对齐到序号之后，整体左对齐
            is_num = body[:1].isdigit() and len(body) > 1 and body[1:2] in (".", "、", "．")
            if is_num:
                tag = "t_num"
            else:
                # 子项（列表符号 / 箭头 / 括号说明）使用更大缩进，与正文段落区分
                is_sub = body[:1] in ("•", "→", "（", "(", "-", "·")
                tag = "t_sub" if is_sub else "t_text"
            if url in body:
                # 长 URL 在窄面板中会逐字断开，改用短文案承载点击行为
                tp.insert("end", self.S["usage_link"], (tag, "t_link"))
            else:
                tp.insert("end", body, tag)
            tp.insert("end", "\n")
            first = False
        tp.configure(state="disabled")

    def _on_usage_wheel(self, e):
        """使用说明区内容较多时，滚轮滚动查看（不显示滚动条）。"""
        self.txt_panel.yview_scroll(-int(e.delta / 120) * 3, "units")
        return "break"

    def _on_usage_motion(self, e):
        self.txt_panel.config(
            cursor="hand2" if "t_link" in self.txt_panel.tag_names("@%d,%d" % (e.x, e.y)) else "arrow")

    # ---------- 扫描与校验 ----------
    def _scan_cfg_map(self):
        """扫描游戏 cfg 目录。每个 cfg 生成多个别名（插件显示名/GUID/文件名），
        映射到真实 cfg 文件名。别名用归一化键，兼容 GUID≠dll 名的第三方 mod。"""
        m = {}
        self._cfg_mtimes = {}
        self._all_by_alias = {}   # alias -> set(cfg文件名)，用于检测同 mod 多份残留
        try:
            if not self.config_dir or not os.path.isdir(self.config_dir):
                return m
            for fn in os.listdir(self.config_dir):
                low = fn.lower()
                if not low.endswith(".cfg") or low == "bepinex.cfg":
                    continue
                path = norm_join(self.config_dir, fn)
                try:
                    mtime = os.path.getmtime(path)
                except Exception:
                    mtime = 0
                for alias in _cfg_aliases(path, fn):
                    self._all_by_alias.setdefault(alias, set()).add(fn)
                    # 同一别名撞多份 cfg（同 mod 新旧残留）时取 mtime 最新的那份，
                    # 保证改的是当前生效的配置，不误改旧版残留。
                    prev = m.get(alias)
                    if prev is None:
                        m[alias] = fn
                        self._cfg_mtimes[alias] = mtime
                    elif mtime > self._cfg_mtimes.get(alias, 0):
                        m[alias] = fn
                        self._cfg_mtimes[alias] = mtime
        except Exception as e:
            log_exc(e)
        return m

    def _cfg_for_dll(self, dll_name):
        """按 dll 名找它对应的 cfg：精确别名命中优先，再做包含匹配兜底。"""
        if not dll_name or not getattr(self, "cfg_map", None):
            return None
        key = _norm_cfg_name(os.path.splitext(dll_name)[0])
        if not key:
            return None
        hit = self.cfg_map.get(key)
        if hit:
            return hit
        # 兜底：dll 名与别名互相包含（如 CafeLargeOrders ↔ largeorders）
        for alias, fn in self.cfg_map.items():
            if (len(alias) >= 3 and len(key) >= 3) and (alias in key or key in alias):
                return fn
        return None

    def _cfg_all_for_dll(self, dll_name):
        """返回某 dll 匹配到的全部 cfg 文件名（去重，用于检测新旧残留）。"""
        if not dll_name or not getattr(self, "_all_by_alias", None):
            return []
        key = _norm_cfg_name(os.path.splitext(dll_name)[0])
        if not key:
            return []
        found = set(self._all_by_alias.get(key, ()))
        for alias, files in self._all_by_alias.items():
            if (len(alias) >= 3 and len(key) >= 3) and (alias in key or key in alias):
                found.update(files)
        return sorted(found)

    def _schema_for(self, cfg_fn):
        """按 mtime 缓存读取单个 cfg 的参数 schema。"""
        path = norm_join(self.config_dir, cfg_fn)
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        hit = self.cfg_schema_cache.get(cfg_fn)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            schema = parse_cfg_schema(path)
        except Exception as e:
            log_exc(e)
            schema = []
        self.cfg_schema_cache[cfg_fn] = (mtime, schema)
        return schema

    def _refresh_paramable(self):
        """对每个已安装 mod 判断：其 cfg 是否含任意可设置项。
        取消所有过滤——布尔/数字/枚举/字符串乃至 debug/日志参数一律列入。"""
        self.paramable = {}
        for dll_name in getattr(self, "_installed_dlls", []):
            cfg_fn = self._cfg_for_dll(dll_name)
            if not cfg_fn:
                continue
            schema = self._schema_for(cfg_fn)
            self.paramable[_norm_cfg_name(os.path.splitext(dll_name)[0])] = bool(schema)

    def _scan(self):
        """扫描游戏 BepInEx/plugins 下所有 mod（单 dll 或含 dll 的目录）。
        启用后缀 .dll，禁用后缀 .dll.disabled；两者都列出，勾选框切换后保存即改名。"""
        self.plugins_files = {}
        self._installed_dlls = []   # 供 _refresh_paramable 判定参数关联
        rows = []
        handle = {}   # 主 dll 名lower -> {name, entries, enabled}
        if os.path.isdir(self.plugins_dir):
            for fn in os.listdir(self.plugins_dir):
                fp = os.path.join(self.plugins_dir, fn)
                try:
                    got = _dll_entries(fp)
                    if not got:
                        continue
                    main, entries = got
                    enabled = any(not dis for _, dis in entries)
                    key = main.lower()
                    # 同名条目（如 Foo.dll 与 Foo.dll.disabled 同时存在）以启用态优先
                    if key in handle and handle[key]["enabled"] and not enabled:
                        continue
                    handle[key] = {"name": main, "entries": entries, "enabled": enabled}
                    self._installed_dlls.append(main)
                except Exception as e:
                    log_exc(e)
        for key in sorted(handle, key=str.lower):
            info = handle[key]
            dll_name = info["name"]
            rows.append({
                "file": key,                 # 主 dll 名 lower（含 .dll），徽章/勾选的稳定 key
                "match_name": dll_name,      # 参数/cfg 关联用的真实 dll 名
                "enabled": info["enabled"],  # True=启用(.dll) / False=禁用(.dll.disabled)
                "status": "enabled" if info["enabled"] else "disabled",
                "cat": None,                 # 通用版无清单
                "tags": [],
                "_entries": info["entries"], # [(真实路径, 是否禁用), ...] 供保存改名
            })
        return rows

    def _display_name(self, r):
        # 通用版无清单：直接用 dll/目录名（去掉 .dll）
        name = r.get("match_name") or r["file"]
        return os.path.splitext(name)[0]

    def _cat_desc_local(self, cat):
        return ""

    def refresh(self):
        self.cfg_map = self._scan_cfg_map()
        try:
            rows = self._scan()          # 先扫描已安装 dll，填充 _installed_dlls
        except Exception as e:
            log_exc(e)
            self.status.config(text=self.S["status_scan_error"].format(err=str(e)))
            return
        self._refresh_paramable()        # 再按 dll 名判定参数关联
        self.rows = rows
        self._render_rows()
        installed = sum(1 for r in rows if r.get("enabled"))
        editable = sum(1 for r in rows if getattr(self, "paramable", {}).get(
            _norm_cfg_name(os.path.splitext(r.get("match_name") or r["file"])[0])))
        self.status.config(text=self.S["universal_status"].format(n=installed, e=editable))
        if not self.game_dir:
            # 未设置游戏目录（通用版发布包里 game_dir 默认为空）：提示先选目录
            self.status.config(text=self.S["need_game_dir"])
        self._check_bepinex()

    # ---------- 校验区（右下角）：只看已启用 / 未启用 ----------
    def _refresh_check_label(self):
        enabled = sum(1 for r in self.rows if r["enabled"])
        disabled = len(self.rows) - enabled
        if self.pending:
            text = self.S["check_pending"].format(e=enabled, d=disabled, n=len(self.pending))
        else:
            text = self.S["check_ok"].format(e=enabled, d=disabled)
        self.status.config(text=text)

    # ---------- 勾选 / 保存 ----------
    def on_save(self):
        """启用/禁用：dll 后缀在 .dll 与 .dll.disabled 间切换。BepInEx 只加载 *.dll。"""
        if not self.pending:
            self.refresh()  # 无变更时兼作手动校验
            return
        if not os.path.isdir(self.game_dir):
            messagebox.showerror(self.S["t_error"], self.S["save_err_game_dir"].format(dir=self.game_dir))
            return
        done, errs = [], []
        name_by_file = {r["file"]: r for r in self.rows}
        for fn, action in list(self.pending.items()):
            r = name_by_file.get(fn)
            if not r:
                continue
            for path, disabled in r.get("_entries", []):
                try:
                    if action == "disable" and not disabled:
                        dst = path + ".disabled"
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.rename(path, dst)
                    elif action == "enable" and disabled:
                        dst = path[:-len(".disabled")] if path.lower().endswith(".disabled") else path
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.rename(path, dst)
                except Exception as e:
                    log_exc(e)
                    errs.append("%s: %s" % (os.path.basename(path), e))
            done.append(self.S["act_enable" if action == "enable" else "act_disable"].format(f=fn))
        self.pending.clear()
        self.refresh()
        if errs:
            messagebox.showerror(self.S["save_err_title"], "\n".join(errs))
            return
        if done:
            messagebox.showinfo(self.S["save_ok_title"], "；".join(done) + "\n\n" + self.S["save_restart"])
        else:
            messagebox.showinfo(self.S["save_ok_title"], self.S["save_ok_empty"])
    # ---------- 删除所选 ----------
    def delete_selected(self):
        return  # 通用版只读，无删除
        if not self.selected:
            messagebox.showinfo(self.S["t_info"], self.S["del_need_sel"])
            return
        sel_set = set(self.selected)
        names = [r["file"] for r in self.rows if r["file"] in sel_set]
        if not names:
            return
        if not messagebox.askyesno(self.S["del_confirm_title"],
                                   self.S["del_confirm_body"].format(names="\n".join(names))):
            return
        os.makedirs(self.trash_dir, exist_ok=True)
        for fn in names:
            try:
                src = os.path.join(self.mods_dir, fn)
                if os.path.exists(src):
                    shutil.move(src, os.path.join(self.trash_dir, fn))
            except Exception as e:
                log_exc(e)
                messagebox.showerror(self.S["t_error"], self.S["del_fail"].format(err=str(e)))
        self.pending.clear()
        self.refresh()

    # ---------- BepInEx 检查 ----------
    def _local_bepinex(self):
        """返回 (是否安装目录存在, 已安装版本或 None, 原因说明)。"""
        if not self.game_dir or not os.path.isdir(self.game_dir):
            return False, None, ""
        bep_dir = os.path.join(self.game_dir, "BepInEx")
        if not os.path.isdir(bep_dir):
            return False, None, ""
        log = os.path.join(bep_dir, "LogOutput.log")
        ver = None
        if os.path.isfile(log):
            try:
                with io.open(log, encoding="utf-8", errors="ignore") as f:
                    head = (f.read(400) or "")
                m = re.search(r"BepInEx\s+(\d+\.\d+\.\d+\.\d+)", head)
                if m:
                    ver = m.group(1)
            except Exception:
                pass
        return True, ver, log

    def _latest_bepinex(self):
        """在线获取最新稳定版 (版本号, 下载直链)。优先 GitHub API，失败/限流时回退抓取 releases 页面；
        均失败返回 None。"""
        def _pick(data):
            url = None
            for a in data.get("assets") or []:
                nm = str(a.get("name") or "")
                if not nm.lower().endswith(".zip"):
                    continue
                low = nm.lower()
                if "x64" in low and "x86" not in low and "linux" not in low and "mac" not in low and "osx" not in low:
                    url = a.get("browser_download_url")
                    break
            return url

        tag = None
        url = None
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/BepInEx/BepInEx/releases/latest",
                headers={"User-Agent": "CafeModManager/1.0"})
            with urllib.request.urlopen(req, timeout=10,
                                        context=ssl.create_default_context()) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            tag = str(data.get("tag_name") or "").strip()
            url = _pick(data)
        except Exception:
            pass

        if not tag or not url:
            # 回退：GitHub API 匿名限流(403)或网络策略拦截时，改用 releases 页面取版本号 + expanded_assets 取包名
            try:
                hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                req = urllib.request.Request(
                    "https://github.com/BepInEx/BepInEx/releases/latest", headers=hdr)
                with urllib.request.urlopen(req, timeout=15,
                                            context=ssl.create_default_context()) as r:
                    html = r.read(524288).decode("utf-8", "ignore")
                m = re.search(r'releases/tag/(v?[\d.]+)', html)
                if m:
                    tag = m.group(1).strip()
                    if tag.lower().startswith("v"):
                        tag = tag[1:]
                if tag:
                    base = "v" + tag if not tag.lower().startswith("v") else tag
                    req = urllib.request.Request(
                        "https://github.com/BepInEx/BepInEx/releases/expanded_assets/" + base,
                        headers=hdr)
                    with urllib.request.urlopen(req, timeout=15,
                                                context=ssl.create_default_context()) as r:
                        frag = r.read(524288).decode("utf-8", "ignore")
                    for nm in re.findall(r'releases/download/[^"\']+/([^"\']+\.zip)', frag):
                        low = nm.lower()
                        if low.endswith(".zip") and "x64" in low and "x86" not in low \
                                and "linux" not in low and "mac" not in low and "osx" not in low:
                            url = ("https://github.com/BepInEx/BepInEx/releases/download/"
                                   + base + "/" + nm)
                            break
            except Exception:
                return None
        if not tag or not url:
            return None
        return tag, url

    def _refresh_bepinex_label(self):
        """仅显示本地当前版本，不做联网校验。"""
        installed, ver, _ = self._local_bepinex()
        if installed:
            base = self.S["bep_installed"].format(v=(ver or "BepInEx"))
        else:
            base = self.S["bep_not_installed"]
        self.lbl_bep.config(text="BepInEx: " + base)

    def install_bepinex(self):
        if not self.game_dir or not os.path.isdir(self.game_dir):
            messagebox.showwarning(self.S["t_warn"],
                                   self.S["bep_setup_first"])
            return
        # 通用版：始终从官方 GitHub releases 在线下载最新版（不随包分发 BepInEx）
        installed, ver, _ = self._local_bepinex()
        if installed:
            tip = self.S["bep_confirm_installed"].format(v=(" " + ver if ver else ""))
        else:
            tip = self.S["bep_confirm_new"]
        if not messagebox.askyesno(self.S["t_confirm"],
                                   tip + "\n\n" + self.S["bep_target"].format(dir=self.game_dir)):
            return
        self.btn_bep.config(state="disabled", text=self.S["btn_install_bep_prog"])
        self.status.config(text=self.S["bep_status_downloading"])
        self._show_prog(self.S["bep_fetch_latest"])

        def _worker():
            try:
                info = self._latest_bepinex()
                if not info:
                    raise RuntimeError(self.S["bep_err_fetch"])
                tag, url = info
                tmp_dir = tempfile.mkdtemp(prefix="bepinex_dl_")
                zip_path = os.path.join(tmp_dir, "BepInEx_x64_%s.zip" % tag)
                try:
                    # 手动分块下载：实时上报百分比，网络超时/中断即失败，避免无限卡住
                    req = urllib.request.Request(url, headers={"User-Agent": "CafeModManager/1.0"})
                    with urllib.request.urlopen(req, timeout=30,
                                                context=ssl.create_default_context()) as r:
                        total = 0
                        try:
                            total = int(r.headers.get("Content-Length") or 0)
                        except Exception:
                            total = 0
                        got = 0
                        chunk = 256 * 1024
                        with open(zip_path, "wb") as f:
                            while True:
                                block = r.read(chunk)
                                if not block:
                                    break
                                f.write(block)
                                got += len(block)
                                if total:
                                    pct = max(0, min(100, int(got * 100 / total)))
                                    self.root.after(0, lambda v=pct, g=got, t=total:
                                                    self._set_prog_download(v, g, t))
                                else:
                                    self.root.after(0, lambda g=got: self._set_prog_bytes(g))
                    self.root.after(0, lambda: self._set_prog_phase(self.S["bep_extracting"]))
                    with zipfile.ZipFile(zip_path) as z:
                        z.extractall(self.game_dir)
                finally:
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                self.root.after(0, lambda: self._install_ok(tag))
            except Exception as e:
                log_exc(e)
                self.root.after(0, lambda _e=e: self._install_fail(str(_e)))

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- BepInEx 安装进度 UI ----------
    def _show_prog(self, text):
        """显示底部进度条区域，开始不确定动画。"""
        self.prog_lbl.config(text=text)
        if not self._prog_visible:
            self.prog_lbl.pack(fill="x", pady=(6, 0))
            self.prog_bar.pack(fill="x", pady=(2, 0))
            self._prog_visible = True
        self.prog_bar.stop()
        self.prog_bar.configure(mode="indeterminate", maximum=100, value=0)
        self.prog_bar.start(12)

    def _set_prog_download(self, pct, got, total):
        self.prog_bar.stop()
        self.prog_bar.configure(mode="determinate", maximum=100, value=pct)
        self.prog_lbl.config(text=self.S["bep_download_pct"].format(
            pct=pct, got=fmt_size(got), total=fmt_size(total)))

    def _set_prog_bytes(self, got):
        self.prog_bar.stop()
        self.prog_bar.configure(mode="indeterminate")
        self.prog_bar.start(12)
        self.prog_lbl.config(text=self.S["bep_download_bytes"].format(got=fmt_size(got)))

    def _set_prog_phase(self, text):
        self.prog_bar.stop()
        self.prog_bar.configure(mode="indeterminate")
        self.prog_bar.start(12)
        self.prog_lbl.config(text=text)

    def _hide_prog(self):
        self.prog_bar.stop()
        self.prog_lbl.pack_forget()
        self.prog_bar.pack_forget()
        self._prog_visible = False

    def _install_ok(self, tag):
        self.btn_bep.config(state="normal", text=self.S["btn_install_bep"])
        self._hide_prog()
        self.status.config(text=self.S["bep_done_status"].format(v=tag))
        self._refresh_bepinex_label()
        messagebox.showinfo(self.S["t_done"], self.S["bep_done_msg"].format(v=tag))

    def _install_fail(self, err):
        self.btn_bep.config(state="normal", text=self.S["btn_install_bep"])
        self._hide_prog()
        self.status.config(text=self.S["bep_fail_status"])
        messagebox.showerror(self.S["bep_fail_title"], err)

    def _install_local_bepinex(self):
        """线程内：把随包自带的本地 BepInEx 复制到游戏根目录，并补齐 Doorstop 注入器。"""
        src = os.path.join(APP_DIR, "BepInEx")
        dst = os.path.join(self.game_dir, "BepInEx")
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied, blocked = self._ensure_doorstop_files()
            self.root.after(0, lambda: self._local_bepinex_ok(copied, blocked))
        except Exception as e:
            log_exc(e)
            self.root.after(0, lambda _e=e: self._local_bepinex_fail(str(_e)))

    def _local_bepinex_ok(self, copied, blocked):
        self.btn_bep.config(state="normal", text=self.S["btn_install_bep"])
        self._hide_prog()
        self.status.config(text=self.S["bep_local_done_status"])
        self._refresh_bepinex_label()
        msg = self.S["bep_copy_done"]
        if copied:
            msg += "\n" + self.S["door_copied"].format(n=copied)
        if blocked:
            msg += "\n" + self.S["door_blocked"].format(n=blocked)
        messagebox.showinfo(self.S["t_done"], msg)

    def _local_bepinex_fail(self, err):
        self.btn_bep.config(state="normal", text=self.S["btn_install_bep"])
        self._hide_prog()
        self.status.config(text=self.S["bep_fail_status"])
        messagebox.showerror(self.S["bep_fail_title"], self.S["bep_copy_fail"].format(err=err))

    def _check_bepinex(self):
        # 通用版不随包带 BepInEx：缺失时只更新标签，安装请点顶部「安装 BepInEx」
        self._refresh_bepinex_label()

    def _copy_bepinex(self):
        # 分离架构：BepInEx 框架随管理器本体分发，源固定为 exe 所在目录（APP_DIR），
        # 不走用户自定义的 mod 根目录。
        src = os.path.join(APP_DIR, "BepInEx")
        dst = os.path.join(self.game_dir, "BepInEx")
        try:
            if not os.path.isdir(src):
                self.root.after(0, lambda: messagebox.showerror(self.S["t_error"], self.S["bep_copy_src_missing"].format(src=src)))
                return
            shutil.copytree(src, dst, dirs_exist_ok=True)
            self.root.after(0, lambda: (self.refresh(),
                                        messagebox.showinfo(self.S["t_done"], self.S["bep_copy_done"])))
        except Exception as e:
            log_exc(e)
            self.root.after(0, lambda _e=e: messagebox.showerror(self.S["t_error"], self.S["bep_copy_fail"].format(err=str(_e))))

    # ---------- Doorstop 注入器自动补齐 ----------
    DOORSTOP_INJECTOR_FILES = (".doorstop_version", "doorstop_config.ini", "winhttp.dll")

    def _ensure_doorstop_files(self):
        """确保游戏根目录存在 Doorstop 注入器三件（缺失才复制，不覆盖已有文件）。

        源：发布包根目录 BepInEx 文件夹内用户放入的三件注入器文件。
        返回 (copied, blocked)；游戏目录/源目录不可用时返回 (0, 0) 静默跳过。
        """
        if not self.game_dir or not os.path.isdir(self.game_dir):
            return 0, 0
        src_dir = os.path.join(APP_DIR, "BepInEx")
        if not os.path.isdir(src_dir):
            return 0, 0
        copied = blocked = 0
        for fn in self.DOORSTOP_INJECTOR_FILES:
            src = os.path.join(src_dir, fn)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(self.game_dir, fn)
            try:
                if os.path.exists(dst):
                    continue
                shutil.copy2(src, dst)   # 游戏运行中 winhttp.dll 可能被锁，失败计入 blocked
                copied += 1
            except Exception as e:
                log_exc(e)
                blocked += 1
        return copied, blocked

    def _auto_ensure_doorstop(self):
        """打开管理器/保存设置后检查注入器三件并自动补齐，结果写入状态栏。"""
        try:
            copied, blocked = self._ensure_doorstop_files()
            if blocked:
                self.status.config(text=self.S["door_blocked"].format(n=blocked))
            elif copied:
                self.status.config(text=self.S["door_copied"].format(n=copied))
        except Exception as e:
            log_exc(e)

    # ---------- cfg 参数修改（参数列 -> 对话框，自动解析真实 cfg） ----------
    def open_param_editor(self, row):
        S = self.S
        cfg_fn = self._cfg_for_dll(row.get("match_name") or row["file"])
        if not cfg_fn:
            messagebox.showinfo(S["param_unavail_title"], S["param_unavail_msg"])
            return
        cfg_rel = "BepInEx/config/" + cfg_fn
        cfg_path = norm_join(self.game_dir, cfg_rel)
        cat = row.get("cat") or {}
        display = self._display_name(row)
        desc = self._cat_desc_local(cat)

        w = tk.Toplevel(self.root)
        w.configure(bg="#ffffff")
        w.title(S["param_win_title"].format(name=display))
        w.geometry("1000x700")
        w.minsize(900, 520)
        w.transient(self.root)
        w.grab_set()

        cfg_exists = os.path.exists(cfg_path)
        # 第3项：备份文件路径（.cfg -> .cfg.bak）
        backup_path = cfg_path + ".bak"
        backup_exists = os.path.exists(backup_path)
        # 第1项：检测同 mod 多份 cfg 残留
        dup_cfgs = self._cfg_all_for_dll(row.get("match_name") or row["file"])
        schema = parse_cfg_schema(cfg_path) if cfg_exists else []
        schema = [p for p in schema if not _is_lang_only_key(p.get("key"))]
        # 参数文字一律按 cfg 原文显示：英文就显示英文，其它语言就显示原文，不做机器翻译

        warn_text = (S["param_warn_restart"] if cfg_exists
                     else S["param_warn_no_cfg"])
        warn = tk.Label(w, text=warn_text,
                        fg="#ffffff", bg="#d64541", font=("Microsoft YaHei UI", 10, "bold"), pady=4)
        warn.pack(fill="x")

        head = ttk.Frame(w)
        head.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Label(head, text=display, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(head, text=S["param_cfg_label"].format(path=cfg_rel.replace("/", "\\\\")),
                  foreground="#0a5fd0").pack(anchor="w", pady=(2, 0))
        ttk.Label(head, text=cfg_path, foreground="#999").pack(anchor="w")
        if desc:
            ttk.Label(head, text=desc, wraplength=940, justify="left", foreground="#333").pack(anchor="w", pady=(4, 0))

        # 第1项：同 mod 多份 cfg 残留提示
        if len(dup_cfgs) > 1:
            tip = S["param_dup_cfg"].format(n=len(dup_cfgs), f=", ".join(dup_cfgs))
            ttk.Label(head, text=tip, wraplength=940, justify="left",
                      foreground="#b45309", background="#fff7e6").pack(anchor="w", fill="x",
                                                                        padx=4, pady=(4, 0))
        # 第3项：已有备份提示
        if cfg_exists and backup_exists:
            tip = S["param_backup_hint"].format(f=os.path.basename(backup_path))
            ttk.Label(head, text=tip, wraplength=940, justify="left",
                      foreground="#0a5fd0").pack(anchor="w", pady=(2, 0))

        if not cfg_exists:
            ttk.Label(w, text=S["param_locked"],
                      foreground="#b00020", wraplength=940).pack(anchor="w", padx=12, pady=4)

        box = ttk.LabelFrame(w, text=S["param_box_title"])
        box.pack(fill="both", expand=True, padx=12, pady=8)
        sf = ScrollFrame(box)
        sf.pack(fill="both", expand=True, padx=4, pady=4)
        inner = sf.inner

        ttk.Label(inner, text=S["param_col_name"], font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(inner, text=S["param_col_default"], font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(inner, text=S["param_col_current"], font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=6, pady=2)
        ttk.Label(inner, text=S["param_col_desc"], font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=3, sticky="w", padx=6, pady=2)

        widgets = []   # (param, ctl)
        state = "normal" if cfg_exists else "disabled"
        row_idx = 1
        last_section = None
        for p in schema:
            section = p.get("section") or ""
            if section and section != last_section:
                sec_txt = section
                ttk.Label(inner, text="■ " + sec_txt, font=("Microsoft YaHei UI", 9, "bold"),
                          foreground="#0a5fd0").grid(row=row_idx, column=0, columnspan=4, sticky="w", padx=4, pady=(8, 0))
                row_idx += 1
                last_section = section
            key = p.get("key", "")
            cur = p.get("current", "")
            default = p.get("default", "")
            ptype = p.get("type", "text")
            choices = p.get("choices") or []
            rng = p.get("range")

            desc_txt = p.get("desc", "") or ""
            tip_full = desc_txt
            pieces = []
            extra = []
            if default:
                pieces.append(S["param_default"].format(d=default))
                extra.append(S["param_tip_default"].format(d=default))
            if rng:
                lo, hi = rng
                lo_s = ("%g" % lo)
                hi_s = ("%g" % hi)
                pieces.append(S["param_range"].format(lo=lo_s, hi=hi_s))
                extra.append(S["param_tip_range"].format(lo=lo_s, hi=hi_s))
            if choices:
                cstr = ", ".join(choices)
                pieces.append(S["param_choices"].format(list=cstr))
                extra.append(S["param_tip_choices"].format(list=cstr))
            if desc_txt:
                pieces.append(S["param_cfg_desc"].format(d=desc_txt))
            if ptype == "hotkey":
                extra.append(S["param_tip_hotkey"])
            short = " | ".join(pieces) if pieces else ""
            if extra:
                tip_full = (tip_full + "\n\n" + "\n".join(extra)) if tip_full else "\n".join(extra)

            lbl = p.get("label") or key
            ttk.Label(inner, text=lbl if lbl == key else "%s（%s）" % (lbl, key),
                      wraplength=250, justify="left").grid(row=row_idx, column=0, sticky="w", padx=4, pady=4)
            ttk.Label(inner, text=default or "—", foreground="#b0b0b0").grid(
                row=row_idx, column=1, sticky="w", padx=6, pady=4)

            ctl = None
            if ptype == "bool":
                cur_bool = str(cur or default).strip().lower() in ("true", "1", "yes")
                sw = Switch(inner, value=cur_bool, text_on=S["sw_on"], text_off=S["sw_off"])
                sw.grid(row=row_idx, column=2, sticky="w", padx=6, pady=4)
                # 说明列只放 cfg 描述；开关自身已显示 true/false，避免两个 label 重叠
                if not cfg_exists:
                    sw.configure(state="disabled")
                    sw.unbind("<Button-1>")
                ctl = sw
            elif ptype == "choice" and choices:
                cb = ttk.Combobox(inner, values=choices, width=16, state="readonly")
                cb.set(cur if cur in choices else (default if default in choices else (choices[0] if choices else "")))
                cb.grid(row=row_idx, column=2, sticky="w", padx=6, pady=4)
                if not cfg_exists:
                    cb.configure(state="disabled")
                ctl = cb
            else:
                e = ttk.Entry(inner, width=22, state=state)
                e.insert(0, cur if cur else (default if not cfg_exists else ""))
                e.grid(row=row_idx, column=2, sticky="w", padx=6, pady=4)
                ctl = e
            if ctl is not None:
                widgets.append((p, ctl))
                if tip_full:
                    tip = ToolTip(ctl)
                    ctl.bind("<Enter>", lambda ev, t=tip_full: tip.show(t, ev.x_root, ev.y_root))
                    ctl.bind("<Leave>", lambda ev: tip.hide())
            ttk.Label(inner, text=short or "—", wraplength=430, foreground="#777", justify="left").grid(
                row=row_idx, column=3, sticky="w", padx=6, pady=4)
            row_idx += 1

        if not schema:
            ttk.Label(inner, text=S["param_no_parse"],
                      foreground="#c77700").grid(row=row_idx, column=0, columnspan=4, sticky="w", padx=4, pady=6)
            row_idx += 1

        def collect():
            values = {}
            errors = []
            for p, ctl in widgets:
                key = p.get("key", "")
                ptype = p.get("type", "text")
                lbl = p.get("label", key)
                if isinstance(ctl, Switch):
                    val = "true" if ctl.get() else "false"
                elif isinstance(ctl, ttk.Combobox):
                    val = ctl.get().strip()
                    choices = p.get("choices") or []
                    if val not in choices:
                        errors.append(S["param_err_choice"].format(label=lbl, key=key, list=", ".join(choices)))
                        continue
                else:
                    val = ctl.get().strip()
                    if ptype == "num":
                        try:
                            float(val)
                        except ValueError:
                            errors.append(S["param_err_num"].format(label=lbl, key=key, v=val))
                            continue
                        rng = p.get("range")
                        if rng:
                            lo, hi = rng
                            if not (lo <= float(val) <= hi):
                                errors.append(S["param_err_range"].format(label=lbl, key=key, v=val,
                                                                          lo=("%g" % lo), hi=("%g" % hi)))
                                continue
                values[key] = val
            return values, errors

        def restore_defaults():
            for p, ctl in widgets:
                default = p.get("default", "")
                if isinstance(ctl, Switch):
                    ctl.set(str(default).strip().lower() in ("true", "1", "yes"))
                elif isinstance(ctl, ttk.Combobox):
                    choices = p.get("choices") or []
                    ctl.set(default if default in choices else (choices[0] if choices else ""))
                else:
                    ctl.delete(0, "end")
                    ctl.insert(0, default)

        def restore_backup():
            # 第3项：从 .bak 恢复整个 cfg（覆盖当前内容），需用户确认
            if not os.path.exists(backup_path):
                messagebox.showinfo(S["param_restore_bak_title"], S["param_restore_bak_none"])
                return
            if not messagebox.askyesno(S["param_restore_bak_title"], S["param_restore_bak_msg"]):
                return
            try:
                shutil.copy2(backup_path, cfg_path)
                self.cfg_schema_cache.pop(cfg_fn, None)   # 失效缓存，下次读新内容
                messagebox.showinfo(S["param_restore_bak_title"], S["param_restore_bak_ok"])
                w.destroy()
            except Exception as e:
                log_exc(e)
                messagebox.showerror(S["t_error"], S["param_save_fail"].format(err=str(e)))

        def save():
            if not cfg_exists:
                messagebox.showwarning(S["param_save_missing_title"], S["param_save_missing_msg"])
                return
            values, errors = collect()
            if errors:
                messagebox.showwarning(S["param_save_warn_title"], "\n".join(errors))
                return
            # 快捷键冲突检查：新设置的热键若已被别的参数占用，先弹窗确认
            hk_items = []
            for p, ctl in widgets:
                if p.get("type") != "hotkey":
                    continue
                k = p.get("key", "")
                hk_items.append((k, p.get("label") or k, values.get(k, "")))
            if hk_items:
                try:
                    label_map = {p.get("key"): (p.get("label") or p.get("key")) for p in schema}
                    conflicts = find_hotkey_conflicts(
                        self.config_dir, cfg_path, hk_items, display,
                        name_of=None,
                        label_of=lambda k: label_map.get(k, k),
                        plugins_dir=self.plugins_dir)
                except Exception as e:
                    log_exc(e)
                    conflicts = []
                if conflicts:
                    lines = [S["hk_conflict_intro"], ""]
                    for c in conflicts:
                        tmpl = S["hk_conflict_self"] if c["kind"] == "self" else S["hk_conflict_other"]
                        lines.append("• " + tmpl.format(label=c["label"], value=c["value"], owner=c["owner"]))
                    lines.append("")
                    lines.append(S["hk_conflict_ask"])
                    if not ask_hotkey_conflict(w, S["hk_conflict_title"], "\n".join(lines),
                                               S["hk_save_anyway"], S["hk_back"],
                                               ok_style="Primary.TButton"):
                        return
            # 第3项：首次保存前自动备份原 cfg，供"从备份恢复"回滚
            try:
                if not os.path.exists(backup_path):
                    shutil.copy2(cfg_path, backup_path)
            except Exception as e:
                log_exc(e)
            changed, added = [], []
            try:
                for p, ctl in widgets:
                    key = p.get("key", "")
                    val = values[key]
                    existed = self._write_cfg_value(cfg_path, key, val, section=p.get("section") or None)
                    (changed if existed else added).append("%s = %s" % (key, val))
                msg = S["param_save_summary"].format(n=len(values)) + "\n"
                if changed:
                    msg += "\n".join("• " + s for s in changed) + "\n"
                if added:
                    msg += S["param_save_added"] + "\n" + "\n".join("• " + s for s in added) + "\n"
                msg += "\n" + S["param_save_file"].format(path=cfg_path)
                messagebox.showinfo(S["param_save_ok_title"], msg)
                w.destroy()
            except Exception as e:
                log_exc(e)
                messagebox.showerror(S["t_error"], S["param_save_fail"].format(err=str(e)))

        btn_row = ttk.Frame(w)
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        if cfg_exists:
            ttk.Button(btn_row, text=S["param_btn_restore"], style="Soft.TButton",
                       command=restore_defaults).pack(side="left")
            if backup_exists:
                ttk.Button(btn_row, text=S["param_btn_restore_bak"], style="Soft.TButton",
                           command=restore_backup).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text=S["cancel"], style="Soft.TButton",
                   command=w.destroy).pack(side="right")
        if cfg_exists:
            ttk.Button(btn_row, text=S["param_btn_save"], style="Primary.TButton",
                       command=save).pack(side="right", padx=8)

    # ---------- cfg 读写 ----------
    def _write_cfg_value(self, cfg_path, key, value, section=None):
        """在 cfg 中替换 key 的值；保留注释、节结构与换行符。返回 True=替换已有项，False=新增。
        指定 section 时先在该节内匹配（避免同名键误替换）；节内无该键或未指定节时全文件兜底。"""
        key_bytes = key.encode("utf-8")
        val_bytes = str(value).encode("utf-8")
        with open(cfg_path, "rb") as f:
            data = f.read()
        nl = b"\r\n" if b"\r\n" in data else b"\n"

        def _sub(scope):
            pat = re.compile(rb"(?m)^(\s*" + re.escape(key_bytes) + rb"\s*=\s*)([^\r\n]*)")
            return pat.sub(lambda mm: mm.group(1) + val_bytes, scope, count=1)

        if section:
            sec_pat = re.compile(rb"(?m)^\s*\[\s*" + re.escape(section.encode("utf-8")) + rb"\s*\]\s*$")
            secs = list(sec_pat.finditer(data))
            for i, m in enumerate(secs):
                end = secs[i + 1].start() if i + 1 < len(secs) else len(data)
                block = data[m.start():end]
                res = _sub(block)
                if res != block:
                    data = data[:m.start()] + res + data[end:]
                    with open(cfg_path, "wb") as f:
                        f.write(data)
                    return True
        # 全文件兜底（节内无该键或未指定节）
        pat = re.compile(rb"(?m)^(\s*" + re.escape(key_bytes) + rb"\s*=\s*)([^\r\n]*)")
        m = pat.search(data)
        if m:
            data = pat.sub(lambda mm: mm.group(1) + val_bytes, data, count=1)
            changed = True
        else:
            data += nl + key.encode("utf-8") + b" = " + val_bytes + nl
            changed = False
        with open(cfg_path, "wb") as f:
            f.write(data)
        return changed

    # ---------- 启动 ----------
    def _detect_mod_root(self):
        """自动探测含 Mods 的 mod 根目录：程序所在目录 -> 上级目录。"""
        for base in (APP_DIR, os.path.dirname(APP_DIR)):
            try:
                if os.path.isdir(os.path.join(base, "Mods")):
                    return base
            except Exception:
                continue
        return ""

    def _bootstrap(self):
        # 通用版：无 mod 根目录概念，直接落盘一次 config
        try:
            self._save_cfg()
        except Exception:
            pass
        self.refresh()
        self._refresh_bepinex_label()
        self._auto_ensure_doorstop()
        # 首启不弹窗：新用户此时多半还没装 BepInEx，引导靠状态栏文字 + readme


_INSTANCE_MUTEX = None  # 保持句柄存活，进程退出前互斥生效


def _already_running():
    """通过命名互斥体保证管理器只允许运行一个实例。"""
    global _INSTANCE_MUTEX
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateMutexW(None, False, "Local\\CafeModManager_SingleInstance")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return True
        _INSTANCE_MUTEX = h
        return False
    except Exception:
        return False


def _activate_existing():
    """尝试把已在运行的窗口带到前台；找不到则返回 False。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main():
    # 单实例：重复启动直接唤起已开窗口并退出本进程
    try:
        if _already_running():
            if not _activate_existing():
                try:
                    _s0 = _lang_sheet((load_json("config.json") or {}).get("lang") or "en")
                    messagebox.showwarning(_s0["t_warn"], _s0["already_running"])
                except Exception:
                    pass
            return
    except Exception:
        pass
    try:
        # DPI 感知：避免高分屏下界面模糊，且窗口坐标与物理像素一致
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        root = tk.Tk()
        apply_style(root)
        # 无边框 + 透明色：四角做圆角（透明色像素镂空）
        try:
            root.overrideredirect(True)
            root.attributes("-transparentcolor", "#ff00f0")
        except Exception:
            root._no_ts = True
        # 无边框窗口默认不会出现在任务栏：移除 WS_EX_TOOLWINDOW 并追加 WS_EX_APPWINDOW
        def _apply_taskbar():
            try:
                root.update_idletasks()
                hwnd = root.winfo_id()
                _parent = ctypes.windll.user32.GetParent(hwnd)
                if _parent:
                    hwnd = _parent
                _gwl = -20  # GWL_EXSTYLE
                _tool = 0x00000080  # WS_EX_TOOLWINDOW：会抑制任务栏按钮，必须清除
                _app = 0x00040000  # WS_EX_APPWINDOW：强制显示任务栏图标
                _ex = ctypes.windll.user32.GetWindowLongPtrW(hwnd, _gwl)
                _new = (_ex & ~_tool) | _app
                if _new != _ex:
                    ctypes.windll.user32.SetWindowLongPtrW(hwnd, _gwl, _new)
                    # 样式变更后用 FRAMECHANGED 让系统重算窗口外观/任务栏归属
                    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                                      0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0100)
            except Exception:
                pass

        # 窗口首次映射/后续装饰重建都可能重置样式，延迟重复应用保证任务栏按钮出现
        try:
            root.after(150, _apply_taskbar)
            root.after(700, _apply_taskbar)
            root.after(1800, _apply_taskbar)
        except Exception:
            pass
        ModManagerApp(root)
        root.mainloop()
    except Exception as e:
        log_exc(e)
        _s0 = _lang_sheet((load_json("config.json") or {}).get("lang") or "en")
        messagebox.showerror(_s0["startup_fail"], _s0["startup_fail_body"].format(err=str(e)))


if __name__ == "__main__":
    main()
