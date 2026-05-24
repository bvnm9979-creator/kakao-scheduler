"""
=============================================================
  kakao_sender.py  —  카카오톡 PC버전 자동 제어 모듈
=============================================================
[중요] 이 모듈은 Windows 전용 라이브러리(pywinauto, pywin32)를 사용합니다.
macOS에서는 import 자체는 성공하지만, 실제 함수 호출은 비활성화됩니다.

[버그 수정 내역]
1. 채팅방이 팝업으로 열릴 때 올바른 창을 찾지 못해 검색란에 메세지가 입력되던 문제 수정
   → GetForegroundWindow()로 실제 열린 채팅 팝업을 감지해 연결
   → 같은 창(탭 모드)인 경우 Escape로 검색 UI 닫은 후 입력
2. 여러 채팅방에 동시 전송 시 서로 충돌해 앞 방이 무시되던 문제 수정
   → threading.Lock()으로 한 번에 한 방씩 순서대로 전송
=============================================================
"""

import os
import io
import sys
import time
import platform
import logging
import threading
import ctypes

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
#  플랫폼 감지 및 Windows 전용 라이브러리 조건부 임포트
# ─────────────────────────────────────────────────────────
IS_WINDOWS: bool = platform.system() == "Windows"
HAS_WIN_DEPS: bool = False

if IS_WINDOWS:
    try:
        import pywinauto
        from pywinauto import Application
        from pywinauto.keyboard import send_keys
        from pywinauto.findwindows import ElementNotFoundError

        import pyperclip

        import win32gui
        import win32con
        import win32clipboard
        import win32api

        from PIL import Image

        HAS_WIN_DEPS = True
        logger.info("Windows 자동화 라이브러리 로드 성공")

    except ImportError as e:
        HAS_WIN_DEPS = False
        logger.warning(f"Windows 자동화 라이브러리 로드 실패: {e}")
else:
    logger.info("비-Windows 환경: 카카오톡 자동화 비활성화")

# ─────────────────────────────────────────────────────────
#  전송 잠금 — 여러 방에 동시 전송 시 충돌 방지
#  한 번에 한 채팅방씩 순서대로 처리
# ─────────────────────────────────────────────────────────
_send_lock = threading.Lock()


# ─────────────────────────────────────────────────────────
#  공개 API: 환경 확인
# ─────────────────────────────────────────────────────────
def check_available() -> tuple:
    if not IS_WINDOWS:
        return False, "Windows 환경에서만 카카오톡 자동 발송이 가능합니다."
    if not HAS_WIN_DEPS:
        return (False,
                "필수 패키지가 없습니다.\n"
                "pip install pywinauto pyperclip pywin32 Pillow 를 실행해주세요.")
    return True, "정상"


# ─────────────────────────────────────────────────────────
#  내부 함수 — 창 탐색
# ─────────────────────────────────────────────────────────
def _find_kakao_hwnd() -> int:
    """
    카카오톡 메인 창 핸들(HWND) 탐색.
    1단계: 정확한 창 제목 매칭
    2단계: 부분 문자열 전체 창 탐색
    """
    for title in ["카카오톡", "KakaoTalk"]:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            logger.debug(f"카카오톡 창 발견 (정확): HWND={hwnd}, 제목='{title}'")
            return hwnd

    found_hwnds = []

    def _enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if "카카오" in title or "kakao" in title.lower():
            found_hwnds.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)

    if found_hwnds:
        logger.debug(f"카카오톡 창 발견 (부분): HWND={found_hwnds[0]}")
        return found_hwnds[0]

    logger.error("카카오톡 창을 찾을 수 없음")
    return 0


def _activate_kakao():
    """
    카카오톡 창을 포그라운드로 활성화하고
    (메인 창 HWND, pywinauto App 객체) 튜플을 반환한다.

    [수정] main_hwnd를 함께 반환하여 _open_chat_room에서
    채팅 팝업 창을 구분하는 데 사용한다.
    """
    hwnd = _find_kakao_hwnd()
    if not hwnd:
        raise RuntimeError(
            "카카오톡 창을 찾을 수 없습니다.\n"
            "카카오톡 PC버전이 실행 중인지 확인해주세요."
        )

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        logger.warning(f"SetForegroundWindow 실패 (무시 가능): {e}")

    time.sleep(0.5)

    app = Application(backend="uia").connect(handle=hwnd)
    return app, hwnd   # ← hwnd도 같이 반환


# ─────────────────────────────────────────────────────────
#  내부 함수 — 클립보드
# ─────────────────────────────────────────────────────────
def _clip_text(text: str):
    """텍스트를 클립보드에 복사 (한글 깨짐 방지)."""
    pyperclip.copy(text)


def _clip_image(image_path: str):
    """이미지를 DIB 형식으로 클립보드에 복사."""
    img = Image.open(image_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    dib_data = buf.getvalue()[14:]
    buf.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_data)
    finally:
        win32clipboard.CloseClipboard()

    logger.debug(f"이미지 클립보드 복사 완료: {os.path.basename(image_path)}")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 마우스 좌표 클릭 헬퍼
# ─────────────────────────────────────────────────────────
def _mouse_click(x: int, y: int):
    """
    화면의 절대 좌표(x, y)를 마우스로 클릭한다.

    pywinauto 컨트롤 탐색 대신 좌표 직접 클릭을 사용하는 이유:
    카카오톡 탭 버튼은 아이콘만 있고 텍스트가 없어서
    pywinauto로 찾을 수 없기 때문.
    """
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.15)


# ─────────────────────────────────────────────────────────
#  내부 함수 — 검색 결과에서 정확한 방 선택
# ─────────────────────────────────────────────────────────
def _select_room_from_results(app: "Application", room_name: str) -> bool:
    """
    검색 결과 ListItem 중 room_name과 가장 잘 맞는 채팅방을 클릭한다.

    [선택 우선순위]
    1순위: 완전 일치  → item_text == room_name
    2순위: 시작 일치  → item_text.startswith(room_name)
           예) "고현석형 외 2명" 이라면 검색어 "고현석형"으로 매칭
    3순위: 포함 일치  → room_name in item_text

    실패(결과 없음 / 오류) 시 False 반환 → 호출자가 ↓Enter fallback 처리.
    """
    try:
        chat_win = app.top_window()
        list_items = chat_win.descendants(control_type="ListItem")

        if not list_items:
            logger.warning("검색 결과 ListItem 없음")
            return False

        # ── 전체 결과 로깅 (디버깅) ──────────────────────────
        texts_found = []
        for item in list_items:
            try:
                t = item.window_text().strip()
                if t:
                    texts_found.append(t)
            except Exception:
                pass
        logger.info(f"검색 결과 목록: {texts_found}")

        # ── 1순위: 완전 일치 ─────────────────────────────────
        for item in list_items:
            try:
                t = item.window_text().strip()
                if t == room_name:
                    item.click_input()
                    logger.info(f"✅ 완전 일치 클릭: '{t}'")
                    return True
            except Exception:
                continue

        # ── 2순위: 시작 일치 ─────────────────────────────────
        for item in list_items:
            try:
                t = item.window_text().strip()
                if t.startswith(room_name):
                    item.click_input()
                    logger.info(f"✅ 시작 일치 클릭: '{t}'")
                    return True
            except Exception:
                continue

        # ── 3순위: 포함 일치 ─────────────────────────────────
        for item in list_items:
            try:
                t = item.window_text().strip()
                if room_name in t:
                    item.click_input()
                    logger.info(f"✅ 포함 일치 클릭: '{t}'")
                    return True
            except Exception:
                continue

        logger.warning(f"'{room_name}'과 일치하는 결과 없음 (결과: {texts_found})")
        return False

    except Exception as e:
        logger.error(f"결과 선택 중 오류: {e}")
        return False


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅방 열기 (좌표 기반)
# ─────────────────────────────────────────────────────────
def _open_chat_room(app: "Application", room_name: str, main_hwnd: int) -> "Application":
    """
    카카오톡 채팅 탭 검색으로 채팅방을 찾아 연다.

    [좌표 기반 클릭 방식 사용 이유]
    카카오톡 탭 버튼(친구/채팅/더보기)은 아이콘만 있고 텍스트가 없어서
    pywinauto로 텍스트 탐색이 불가능하다.
    창 크기에서 비율로 계산한 좌표를 직접 클릭한다.

    [카카오톡 PC 화면 구조]
    ┌──────────────────────────────────────┐ ← top
    │ 사이드바│   채팅 ▼         🔍   C+  │ ← 🔍: right-130, top+55
    │  👤     ├──────────────────────────  │
    │  💬 ←채팅 탭(창 높이 30%)           │
    │  ...    │   채팅방 목록              │
    └──────────────────────────────────────┘
      ↑ +75px

    [동작 순서]
    1. 채팅 탭 아이콘 좌표 클릭 (사이드바 두 번째 아이콘)
    2. 🔍 검색 버튼 좌표 클릭 (우상단)
    3. 채팅방 이름 클립보드 붙여넣기
    4. ↓ + Enter 키로 첫 번째 결과 선택 (채팅방만 표시됨)
    5. 팝업 창이면 재연결
    """
    main_win = app.top_window()
    main_win.set_focus()
    time.sleep(0.4)

    # ── 창 위치·크기 계산 ────────────────────────────────────
    left, top, right, bottom = win32gui.GetWindowRect(main_hwnd)
    width  = right  - left
    height = bottom - top

    # ── Step 1: 채팅 탭 클릭 ──────────────────────────────────
    # 사이드바 중앙 x ≈ 창 왼쪽 + 75px
    # 채팅 아이콘 y ≈ 창 높이의 30% (사이드바 두 번째 아이콘)
    chat_tab_x = left + 75
    chat_tab_y = top  + int(height * 0.30)
    _mouse_click(chat_tab_x, chat_tab_y)
    logger.info(f"채팅 탭 클릭: ({chat_tab_x}, {chat_tab_y})")
    time.sleep(0.5)

    # ── Step 2: 🔍 검색 버튼 클릭 ────────────────────────────
    # x ≈ 창 오른쪽 - 130px  /  y ≈ 창 위쪽 + 55px
    search_x = right - 130
    search_y = top   + 55
    _mouse_click(search_x, search_y)
    logger.info(f"검색 버튼 클릭: ({search_x}, {search_y})")
    time.sleep(0.5)

    # ── Step 3: 채팅방 이름 입력 ─────────────────────────────
    # 검색창이 열린 상태에서 클립보드로 붙여넣기 (한글 안전)
    _clip_text(room_name)
    send_keys("^v")
    time.sleep(1.2)   # 검색 결과 로딩 대기
    logger.info(f"'{room_name}' 검색 입력 완료")

    # ── Step 4: 정확한 채팅방 선택 ──────────────────────────
    # pywinauto로 검색 결과 ListItem을 읽어 이름이 일치하는 방 클릭
    # 실패 시 ↓Enter fallback(첫 번째 결과)
    selected = _select_room_from_results(app, room_name)
    if not selected:
        logger.warning(f"일치하는 결과 없음 → ↓Enter로 첫 번째 결과 선택")
        send_keys("{DOWN}")
        time.sleep(0.3)
        send_keys("{ENTER}")
    time.sleep(1.2)

    # ── Step 5: 팝업 창이면 재연결 ────────────────────────────
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg_hwnd)
    logger.info(f"열린 창: '{fg_title}' (HWND={fg_hwnd})")

    if fg_hwnd and fg_hwnd != main_hwnd:
        try:
            chat_app = Application(backend="uia").connect(handle=fg_hwnd)
            logger.info("채팅 팝업 창 연결 성공")
            return chat_app
        except Exception as e:
            logger.warning(f"팝업 연결 실패: {e} → 메인 창 사용")

    # 탭 모드: 검색창 Escape로 닫기
    send_keys("{ESCAPE}")
    time.sleep(0.3)
    return app


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅 입력창 탐색
# ─────────────────────────────────────────────────────────
def _find_input_ctrl(app: "Application"):
    """채팅 입력창 컨트롤 탐색 (Edit → Document → RichEdit 순 fallback)."""
    chat_win = app.top_window()

    for ctrl_type in ["Edit", "Document", "RichEdit20W", "RICHEDIT50W"]:
        try:
            candidates = chat_win.descendants(control_type=ctrl_type)
            # 활성화된 컨트롤만 필터링
            enabled = [c for c in candidates if c.is_enabled()]
            if enabled:
                return enabled[-1]   # 마지막 = 하단 입력창
        except Exception:
            continue

    raise RuntimeError(
        "채팅 입력창을 찾을 수 없습니다.\n"
        "카카오톡 버전 업데이트 후 UI 구조가 바뀌었을 수 있습니다."
    )


# ─────────────────────────────────────────────────────────
#  내부 함수 — 텍스트 전송
# ─────────────────────────────────────────────────────────
def _send_text(app: "Application", message: str):
    """
    채팅창에 텍스트를 입력하고 전송한다.
    줄바꿈은 Shift+Enter로 처리 (Enter는 전송).
    한글은 클립보드 붙여넣기 방식으로 IME 문제 우회.
    """
    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.3)

    try:
        ctrl = _find_input_ctrl(app)
        ctrl.click_input()
        time.sleep(0.2)
    except Exception as e:
        logger.warning(f"입력창 클릭 실패 → Tab 대체: {e}")
        send_keys("{TAB}")
        time.sleep(0.2)

    lines = message.split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            _clip_text(line)
            send_keys("^v")
            time.sleep(0.15)

        if i < len(lines) - 1:
            send_keys("+{ENTER}")
            time.sleep(0.1)

    time.sleep(0.2)
    send_keys("{ENTER}")
    time.sleep(0.5)
    logger.info("텍스트 전송 완료")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 이미지 전송
# ─────────────────────────────────────────────────────────
def _send_image(app: "Application", image_path: str):
    """이미지를 클립보드 경유로 채팅창에 전송한다."""
    if not os.path.isfile(image_path):
        logger.warning(f"이미지 파일 없음, 건너뜀: {image_path}")
        return

    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.2)

    _clip_image(image_path)
    time.sleep(0.3)

    try:
        ctrl = _find_input_ctrl(app)
        ctrl.click_input()
        time.sleep(0.2)
    except Exception as e:
        logger.warning(f"입력창 클릭 실패 → Tab 대체: {e}")
        send_keys("{TAB}")
        time.sleep(0.2)

    send_keys("^v")
    time.sleep(0.8)
    send_keys("{ENTER}")
    time.sleep(0.6)
    logger.info(f"이미지 전송 완료: {os.path.basename(image_path)}")


# ─────────────────────────────────────────────────────────
#  공개 API — 메인 발송 함수
# ─────────────────────────────────────────────────────────
def send_to_room(room_name: str, message: str, image_path: str = None) -> dict:
    """
    카카오톡 특정 채팅방에 텍스트 메시지와/또는 이미지를 전송한다.

    [수정] threading.Lock으로 감싸서 여러 채팅방에 순서대로 전송.
    기존에는 동시 전송 시도로 충돌 → 첫 번째 방이 무시되는 버그 수정.
    """
    ok, reason = check_available()
    if not ok:
        logger.warning(f"자동화 불가 환경: {reason}")
        return {"success": False, "error": reason}

    # ── 순서 보장 잠금 — 한 번에 한 방씩 전송 ──
    with _send_lock:
        try:
            logger.info(f"▶ 발송 시작 → 채팅방: '{room_name}'")

            # 1. 카카오톡 메인 창 활성화 (hwnd도 받아옴)
            app, main_hwnd = _activate_kakao()
            time.sleep(0.5)

            # 2. 채팅방 검색·열기 (올바른 창에 연결된 app 반환)
            chat_app = _open_chat_room(app, room_name, main_hwnd)
            time.sleep(0.6)

            # 3. 텍스트 전송
            if message and message.strip():
                _send_text(chat_app, message)
                time.sleep(0.5)

            # 4. 이미지 전송
            if image_path:
                _send_image(chat_app, image_path)

            logger.info(f"✅ 발송 완료 → 채팅방: '{room_name}'")
            return {"success": True, "error": ""}

        except RuntimeError as e:
            logger.error(f"❌ 발송 실패 [{room_name}]: {e}")
            return {"success": False, "error": str(e)}

        except Exception as e:
            logger.error(f"❌ 예상치 못한 오류 [{room_name}]: {e}", exc_info=True)
            return {"success": False, "error": f"예상치 못한 오류: {e}"}
