# -*- coding: utf-8 -*-
"""
微信「图片和视频」窗口自动处理脚本（A + B 合并版，可用参数跳过单项）

每轮循环：
  0) 按右键 -> 下一个对象（若保存窗还开着，等它关闭，不抢焦点）
  1) 截取窗口顶部 TOP_BAR_HEIGHT 像素横条
  2) 功能 A（ENABLE_A）：模板匹配 video.PNG「查看原视频」-> 点击 -> 等待 WAIT_VIDEO 秒
  3) 功能 B（ENABLE_B）：SIFT 特征匹配 tpl_ratio.png（尺度不变） ->
        Ctrl+S 调出保存窗 -> Alt+S 确认 -> 等待（不点击、不与保存窗抢焦点）
  4) A/B 都未触发 -> 跳过
  随时按 ESC 停止。

开关（脚本顶部）：
  ENABLE_A = True / False     # 开/关「查看原视频」自动点击
  ENABLE_B = True / False     # 开/关 1:1 模板匹配 + 保存

依赖：pip install pillow numpy opencv-python
模板：video.PNG（查看原视频）、tpl_ratio.png（1:1）—— 放脚本同目录
匹配算法：SIFT 优先（尺度不变），失败自动降级多尺度模板匹配（NCC）。
"""

import ctypes
import os
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
ENABLE_A = True      # A：匹配 video.PNG「查看原视频」并点击
ENABLE_B = True      # B：匹配 tpl_ratio.png 后发 Ctrl+S -> Alt+S 保存

# --- 功能 A 参数 ---
VIDEO_TPL = "video.PNG"      # 触发模板：查看原视频按钮
WAIT_VIDEO = 2.0             # 点击后等待秒数
CLICK_Y_OFFSET = 8           # 点击点相对模板中心的下移量

# --- 功能 B 参数 ---
TRIGGER_TPL = "tpl_ratio.png"   # 触发模板：1:1 按钮
CONFIRM_KEY = "alt_s"           # 保存窗确认键："alt_s" | "enter"
# --- SIFT 特征匹配参数（尺度不变）---
SIFT_MIN_MATCHES = 4       # 最少有效特征匹配点数（低于此视为未命中）
SIFT_RATIO = 0.72          # Lowe 比值测试阈值（越小越严格）
SIFT_TPL_UPSCALE = 3.0     # 模板预放大倍数（小图标提特征用）
POST_RIGHT_DELAY = 0.2
AFTER_SAVE_DELAY = 0.8          # Ctrl+S 后等保存窗出现
AFTER_ENTER_DELAY = 0.8         # Alt+S 后等保存窗关闭

# --- 通用 ---
ESC_POLL = 0.1
DEBUG_SAVE_IMAGE = False
DEBUG_IMAGE_PATH = "debug_hit.png"

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


def wait_foreground(hwnd, timeout=10):
    """等待前台回到主窗口（保存窗自行关闭），绝不抢焦点。"""
    start = time.time()
    while not is_foreground(hwnd):
        if esc_pressed():
            return False
        if time.time() - start > timeout:
            return False
        time.sleep(ESC_POLL)
    return True


# ========================= 模板匹配（SIFT + NCC 兜底）=========================
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


_sift = None


def _get_sift():
    global _sift
    if _sift is None:
        _sift = cv2.SIFT_create()
    return _sift


def _sift_match(bar_bgr, tpl):
    """SIFT 特征匹配，返回 (dx, dy, inliers, scale) 或 (None,None,n,0)。"""
    sift = _get_sift()

    # 模板先放大，让小图标提取到足够特征点
    big = cv2.resize(tpl, None, fx=SIFT_TPL_UPSCALE, fy=SIFT_TPL_UPSCALE,
                     interpolation=cv2.INTER_CUBIC)
    kp1, des1 = sift.detectAndCompute(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY), None)
    kp2, des2 = sift.detectAndCompute(cv2.cvtColor(bar_bgr, cv2.COLOR_BGR2GRAY), None)
    if des1 is None or des2 is None or len(kp1) < 2 or len(kp2) < 2:
        return (None, None, 0, 0.0)

    # KNN + Lowe 比值测试
    matches = cv2.BFMatcher().knnMatch(des1, des2, k=2)
    good = [m for m, n in matches if m.distance < SIFT_RATIO * n.distance]
    if len(good) < SIFT_MIN_MATCHES:
        return (None, None, len(good), 0.0)

    # RANSAC 求单应矩阵，剔除误匹配
    src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2) / SIFT_TPL_UPSCALE
    dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None or mask is None:
        return (None, None, len(good), 0.0)

    inliers = int(mask.sum())
    if inliers < SIFT_MIN_MATCHES:
        return (None, None, inliers, 0.0)

    # 模板四角投影到场景，取中心 + 估算缩放
    h, w = tpl.shape[:2]
    corners = cv2.perspectiveTransform(
        np.float32([[[0, 0], [w, 0], [w, h], [0, h]]]), H).reshape(-1, 2)
    cx, cy = corners.mean(axis=0)
    # 用上下边长估算缩放比（相对模板原始尺寸）
    top_edge = np.linalg.norm(corners[1] - corners[0])
    scale_est = top_edge / w
    return (float(cx), float(cy), inliers, float(scale_est))


def _scaled_template_match(bar_bgr, tpl):
    """多尺度归一化互相关匹配（SIFT 失败时的兜底，对小图标很有效）。
    返回 (dx, dy, score, scale) 或 (None, None, best_score, 0)。
    """
    best = None
    for s in [0.4 + i * 0.02 for i in range(51)]:  # 0.40 .. 1.40
        t = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
        if bar_bgr.shape[0] < t.shape[0] or bar_bgr.shape[1] < t.shape[1]:
            continue
        res = cv2.matchTemplate(bar_bgr, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[2]:
            best = (loc[0] + t.shape[1] / 2.0, loc[1] + t.shape[0] / 2.0, mx, s)
    if best is None:
        return (None, None, 0.0, 0.0)
    if best[2] >= 0.75:
        return best
    return (None, None, best[2], 0.0)


def match_template(bar_bgr, tpl):
    """SIFT 优先（尺度不变），失败则降级多尺度模板匹配。
    返回 (dx, dy, method, confidence, scale)；未命中 dx=dy=None。
    """
    if tpl is None:
        return (None, None, "none", 0.0, 0.0)
    # 1) SIFT
    sx, sy, conf, scale = _sift_match(bar_bgr, tpl)
    if sx is not None:
        return (sx, sy, "SIFT", conf, scale)
    # 2) 兜底：多尺度模板匹配
    tx, ty, conf, scale = _scaled_template_match(bar_bgr, tpl)
    if tx is not None:
        return (tx, ty, "NCC", conf, scale)
    return (None, None, "NCC", conf, scale)


# ========================= 主循环 =========================
def main():
    # 按开关加载所需资源
    tpl_trigger = tpl_video = None
    if ENABLE_B:
        tpl_trigger = load_template(TRIGGER_TPL)
        if tpl_trigger is None:
            print("功能 B 已开启但模板缺失，请检查 %s" % TRIGGER_TPL)
            return 1
        print("B 模板已加载: %s (%dx%d)" % (
            TRIGGER_TPL, tpl_trigger.shape[1], tpl_trigger.shape[0]))
    if ENABLE_A:
        tpl_video = load_template(VIDEO_TPL)
        if tpl_video is None:
            print("功能 A 已开启但模板缺失，请检查 %s" % VIDEO_TPL)
            return 1
        print("A 模板已加载: %s (%dx%d)" % (
            VIDEO_TPL, tpl_video.shape[1], tpl_video.shape[0]))

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
        if esc_pressed():
            break

        # 0) 下一张：若前台不是主窗口（保存窗还开着），等它关闭，不抢焦点
        if not wait_foreground(hwnd):
            if esc_pressed():
                break
            print("前台窗口未恢复，跳过本轮（不抢焦点）。")
            continue
        press_right()
        n += 1
        if not interruptible_sleep(POST_RIGHT_DELAY):
            break

        # 1) 截取顶部横条
        left, top, right, bottom = get_rect(hwnd)
        crop_h = min(TOP_BAR_HEIGHT, bottom - top)
        if crop_h <= 0 or right <= left:
            print("[%d] 窗口区域异常，跳过。" % n)
            continue
        img = ImageGrab.grab(bbox=(left, top, right, top + crop_h))
        bar_bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)

        handled = False

        # 2) 功能 A：模板匹配「查看原视频」-> 点击
        if ENABLE_A and not handled:
            vx, vy, method, conf, scale_est = match_template(bar_bgr, tpl_video)
            print("[%d] A 匹配[%s] conf=%.2f scale=%.2f" % (n, method, conf, scale_est))
            if vx is not None:
                click_x = left + vx
                click_y = top + vy + CLICK_Y_OFFSET
                print("[%d] A 命中「查看原视频」@(%.0f,%.0f) -> 点击后等待 %.1fs。" %
                      (n, click_x, click_y, WAIT_VIDEO))
                if DEBUG_SAVE_IMAGE:
                    try:
                        img.save(DEBUG_IMAGE_PATH)
                    except Exception:
                        pass
                click(click_x, click_y)
                count_a += 1
                handled = True
                if not interruptible_sleep(WAIT_VIDEO):
                    break

        # 3) 功能 B：SIFT(优先) / 多尺度模板(兜底) 匹配 -> Ctrl+S -> Alt+S（不点击、不抢焦点）
        if ENABLE_B and not handled:
            tx, ty, method, conf, scale_est = match_template(bar_bgr, tpl_trigger)
            if DEBUG_SAVE_IMAGE:
                try:
                    img.save(DEBUG_IMAGE_PATH)
                except Exception:
                    pass
            print("[%d] B 匹配[%s] conf=%.2f scale=%.2f" % (n, method, conf, scale_est))
            if tx is not None:
                print("[%d] B 命中触发模板 @(%.0f,%.0f) [%s conf=%.2f] -> Ctrl+S" %
                      (n, left + tx, top + ty, method, conf))
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

        if not handled:
            print("[%d] 无匹配 -> 跳过。" % n)

    print("已停止（ESC）。共 %d 轮，A 触发 %d 次，B 触发 %d 次。"
          % (n, count_a, count_b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
