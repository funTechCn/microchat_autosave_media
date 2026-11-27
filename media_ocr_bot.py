# -*- coding: utf-8 -*-
"""
微信「图片和视频」窗口自动处理脚本（A + B 合并版，可用参数跳过单项）

每轮循环：
  0) 按右键 -> 下一个对象（若保存窗还开着，等它关闭，不抢焦点）
  1) 截取窗口顶部 TOP_BAR_HEIGHT 像素横条
  2) 功能 A（ENABLE_A）：OCR 识别「查看原视频」-> 点击该按钮 -> 等待 WAIT_VIDEO 秒
  3) 功能 B（ENABLE_B）：模板匹配 tpl_ratio.png（1:1） ->
        Ctrl+S 调出保存窗 -> Alt+S 确认 -> 等待（不点击、不与保存窗抢焦点）
  4) A/B 都未触发 -> 跳过
  随时按 ESC 停止。

开关（脚本顶部）：
  ENABLE_A = True / False     # 开/关「查看原视频」OCR 自动点击
  ENABLE_B = True / False     # 开/关 1:1 模板匹配 + 保存

依赖：pip install pillow numpy opencv-python rapidocr onnxruntime
模板：tpl_ratio.png（1:1 触发图）—— 放脚本同目录
      （A 用 OCR 识别文字，不需要 video 模板）
"""

import ctypes
import os
import re
import sys
import time
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import numpy as np
    import cv2
    from PIL import ImageGrab
except ImportError:
    print("缺少基础依赖，请先执行: pip install pillow numpy opencv-python")
    raise

# ========================= 可配置参数 =========================
WINDOW_TITLE = "图片和视频"
TOP_BAR_HEIGHT = 52

# --- A/B 功能开关（用参数控制跳过单项）---
ENABLE_A = True      # A：OCR 识别「查看原视频」并点击
ENABLE_B = True     # B：匹配 tpl_ratio.png 后发 Ctrl+S -> Alt+S 保存

# --- 功能 A 参数 ---
OCR_SCALE = 1.0          # OCR 放大倍数：实测 1.0 识别「查看原视频」准确且最快（0.86s），3.0 会慢到 8s
WAIT_VIDEO = 2.0         # 点击后等待秒数
CLICK_Y_OFFSET = 8       # 点击点相对文字框中心的下移量

# --- 功能 B 参数 ---
TRIGGER_TPL = "tpl_ratio.png"   # 触发模板：1:1 按钮
CONFIRM_KEY = "alt_s"           # 保存窗确认键："alt_s" | "enter"
NCC_THRESHOLD = 0.75            # 1:1 模板匹配阈值
POST_RIGHT_DELAY = 0.2           # 右键后等待（防截图太早）
RETRY_DELAY = 0.35               # 首轮未识别时的重试等待（下一张图渲染慢）
OCR_RETRY = True                 # 是否启用截图重试
AFTER_SAVE_DELAY = 0.8          # Ctrl+S 后等保存窗出现
AFTER_ENTER_DELAY = 0.8         # Alt+S 后等保存窗关闭

# --- 通用 ---
ESC_POLL = 0.1
DEBUG_SAVE_IMAGE = False
DEBUG_IMAGE_PATH = "debug_hit.png"
DEBUG_OCR_TEXT = True    # 打印每轮 OCR 识别到的全部文本（排查漏识别）

# ========================= Win32 封装 =========================
user32 = ctypes.windll.user32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

VK_RIGHT = 0x27
VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_MENU = 0x12      # Alt
VK_S = 0x53
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SW_RESTORE = 9


def find_window(title):
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        return hwnd
    matches = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                if title in buf.value:
                    matches.append((h, buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    if matches:
        for h, name in matches:
            print("匹配到窗口: %s" % name)
        return matches[0][0]
    return None


def get_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def is_foreground(hwnd):
    return user32.GetForegroundWindow() == hwnd


def activate(hwnd):
    user32.SetForegroundWindow(hwnd)


def press_right():
    user32.keybd_event(VK_RIGHT, 0, 0, 0)
    user32.keybd_event(VK_RIGHT, 0, KEYEVENTF_KEYUP, 0)


def press_key(key):
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def hotkey(mod, key):
    """组合键：纯键盘事件，不改窗口焦点。"""
    user32.keybd_event(mod, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def move_mouse_away():
    """把鼠标移出工具栏区域，避免 hover 高亮/遮挡影响 OCR。"""
    # 移到窗口底部中间的空白区（远离顶部 52px 工具栏）
    user32.SetCursorPos(10, user32.GetSystemMetrics(1) - 80)


def esc_pressed():
    return (user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000) != 0


def interruptible_sleep(seconds):
    end = time.time() + seconds
    while True:
        if esc_pressed():
            return False
        remaining = end - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(ESC_POLL, remaining))


def _fg_title():
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    if n:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        return buf.value
    return "(无标题)"


def wait_foreground(hwnd, timeout=10, quiet=False):
    """等待前台回到主窗口（保存窗自行关闭），绝不抢焦点。"""
    start = time.time()
    while not is_foreground(hwnd):
        if esc_pressed():
            return False
        waited = time.time() - start
        if waited > timeout:
            if not quiet:
                print("    [诊断] wait_foreground 超时 %.1fs，当前前台: %s" % (waited, _fg_title()))
            return False
        time.sleep(ESC_POLL)
    waited = time.time() - start
    if waited > 0.2 and not quiet:
        print("    [诊断] wait_foreground 等待 %.1fs，前台: %s" % (waited, _fg_title()))
    return True


# ========================= 功能 A：OCR =========================
_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            print("功能 A 已开启但缺少 rapidocr，请执行: pip install rapidocr onnxruntime")
            raise SystemExit(1)
        _ocr = RapidOCR()
    return _ocr


def ocr_video_button(img, base_x, base_y):
    """OCR 只识别横条右侧 1/4（「查看原视频」按钮固定在右侧），返回 (命中, x, y, 文本, 所有文本)。"""
    from PIL import Image
    scale = OCR_SCALE
    # 只取右侧 1/4：2560px -> 640px，OCR 更快
    crop_x = int(img.width * 0.75)
    crop = img.crop((crop_x, 0, img.width, img.height))
    if scale != 1.0:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
    res = get_ocr()(np.asarray(crop))
    all_texts = list(res.txts or [])
    for txt, box in zip(all_texts, res.boxes if res.boxes is not None else []):
        n = re.sub(r"\s+", "", str(txt)).replace("：", ":")
        # 严格匹配「查看原视频」（必须四字齐全，不放宽为仅「原视频」）
        if "查看原视频" in n:
            c = np.asarray(box).reshape(-1, 2).mean(axis=0)
            # 换算回屏幕坐标：裁剪区起点 + 框中心
            return (True, base_x + crop_x + c[0] / scale,
                    base_y + c[1] / scale + CLICK_Y_OFFSET, txt, all_texts)
    return (False, 0, 0, "", all_texts)


# ========================= 功能 B：模板匹配 =========================
def _resolve(name):
    if os.path.isabs(name):
        return name
    base = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    return os.path.join(base, name)


def load_template(name):
    path = _resolve(name)
    if not os.path.exists(path):
        print("找不到模板图 %s" % path)
        return None
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def match_icon(bar_bgr, tpl):
    """多尺度 NCC 匹配 1:1 图标（尺度不变兜底），返回 (dx, dy, score) 或 (None,None,score)。"""
    best = None
    for s in [0.4 + i * 0.02 for i in range(51)]:
        t = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
        if bar_bgr.shape[0] < t.shape[0] or bar_bgr.shape[1] < t.shape[1]:
            continue
        res = cv2.matchTemplate(bar_bgr, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[2]:
            best = (loc[0] + t.shape[1] / 2.0, loc[1] + t.shape[0] / 2.0, mx)
    if best is None:
        return (None, None, 0.0)
    if best[2] >= NCC_THRESHOLD:
        return best
    return (None, None, best[2])


# ========================= 主循环 =========================
def main():
    tpl_trigger = None
    if ENABLE_B:
        tpl_trigger = load_template(TRIGGER_TPL)
        if tpl_trigger is None:
            print("功能 B 已开启但模板缺失，请检查 %s" % TRIGGER_TPL)
            return 1
        print("B 模板已加载: %s (%dx%d)" % (
            TRIGGER_TPL, tpl_trigger.shape[1], tpl_trigger.shape[0]))
    if ENABLE_A:
        get_ocr()
        print("A（查看原视频 OCR）已启用")

    hwnd = find_window(WINDOW_TITLE)
    if not hwnd:
        print("找不到标题为「%s」的窗口，请先打开它。" % WINDOW_TITLE)
        return 1

    activate(hwnd)
    print("已就绪：A=%s B=%s。5 秒后开始，请把焦点切到窗口；随时按 ESC 停止。"
          % ("开" if ENABLE_A else "跳过", "开" if ENABLE_B else "跳过"))
    if not interruptible_sleep(5.0):
        print("已停止（ESC）。")
        return 0
    activate(hwnd)

    n = 0
    count_a = 0
    count_b = 0
    while True:
        t_round = time.time()
        if esc_pressed():
            break

        # 0) 下一张。B 功能（保存窗）需等前台恢复不抢焦点；
        #    纯 A（OCR 点击）无保存窗，点击后可能弹出播放器占前台，直接激活抢回，避免空等变慢。
        if ENABLE_B:
            if not wait_foreground(hwnd):
                if esc_pressed():
                    break
                print("前台窗口未恢复，跳过本轮（不抢焦点）。")
                continue
        elif not is_foreground(hwnd):
            activate(hwnd)
        press_right()
        n += 1
        if not interruptible_sleep(POST_RIGHT_DELAY):
            break

        # 1) 截取顶部横条。窗口被最小化则还原（不改最大化状态），再重取坐标。
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
        left, top, right, bottom = get_rect(hwnd)
        crop_h = min(TOP_BAR_HEIGHT, bottom - top)
        if crop_h <= 0 or right <= left:
            print("[%d] 窗口区域异常，跳过。" % n)
            continue
        # 移开鼠标并等待 hover 高亮消退，避免遮挡/改变按钮外观
        move_mouse_away()
        time.sleep(0.2)
        img = ImageGrab.grab(bbox=(left, top, right, top + crop_h))
        bar_bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)

        handled = False

        # 2) 功能 B 优先：模板匹配 1:1（毫秒级）-> Ctrl+S -> Alt+S（不点击、不抢焦点）
        #    图片界面有 1:1 按钮、视频界面有「查看原视频」，二者互斥。
        #    B 先判断可避免图片界面白跑昂贵的 OCR（曾导致每轮 20 秒）。
        if ENABLE_B and not handled:
            bx, by, score = match_icon(bar_bgr, tpl_trigger)
            if bx is not None:
                print("[%d] B 命中 1:1 conf=%.2f @(%.0f,%.0f) -> Ctrl+S" %
                      (n, score, left + bx, top + by))
                if DEBUG_SAVE_IMAGE:
                    try:
                        img.save(DEBUG_IMAGE_PATH)
                    except Exception:
                        pass
                hotkey(VK_CONTROL, VK_S)
                if not interruptible_sleep(AFTER_SAVE_DELAY):
                    break
                if CONFIRM_KEY == "enter":
                    press_key(VK_RETURN)
                else:
                    hotkey(VK_MENU, VK_S)
                count_b += 1
                handled = True
                print("    已保存（累计 B %d 张）。" % count_b)
                if not interruptible_sleep(AFTER_ENTER_DELAY):
                    break

        # 3) 功能 A：B 未命中（视频界面）才跑 OCR 识别「查看原视频」-> 点击
        if ENABLE_A and not handled:
            ok, ax, ay, txt, all_texts = ocr_video_button(img, left, top)
            if DEBUG_OCR_TEXT:
                print("    [OCR] " + " | ".join(str(t) for t in all_texts))
            # 未识别到时：可能截图太早（下张图还没渲染完），等待后重截一次
            if not ok and OCR_RETRY:
                if not interruptible_sleep(RETRY_DELAY):
                    break
                move_mouse_away()
                img = ImageGrab.grab(bbox=(left, top, right, top + crop_h))
                ok, ax, ay, txt, all_texts = ocr_video_button(img, left, top)
                if DEBUG_OCR_TEXT:
                    print("    [OCR重试] " + " | ".join(str(t) for t in all_texts))
            if ok:
                print("[%d] A 命中「%s」@(%.0f,%.0f) -> 点击后等待 %.1fs。" %
                      (n, txt, ax, ay, WAIT_VIDEO))
                if DEBUG_SAVE_IMAGE:
                    try:
                        img.save(DEBUG_IMAGE_PATH)
                    except Exception:
                        pass
                click(ax, ay)
                count_a += 1
                handled = True
                if not interruptible_sleep(WAIT_VIDEO):
                    break

        if not handled:
            print("[%d] 无匹配 -> 跳过。" % n)
        print("    [耗时] 本轮 %.1fs" % (time.time() - t_round))

    print("已停止（ESC）。共 %d 轮，A 触发 %d 次，B 触发 %d 次。"
          % (n, count_a, count_b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
