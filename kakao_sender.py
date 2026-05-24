"""
=============================================================
  kakao_sender.py  —  카카오톡 PC버전 자동 제어 모듈
=============================================================
[중요] 이 모듈은 Windows 전용 라이브러리(pywinauto, pywin32)를 사용합니다.
macOS에서는 import 자체는 성공하지만, 실제 함수 호출은 비활성화됩니다.

[기능 개선 내역]
1. 채팅방 검색 완전 일치 보장
   → 정확히 일치하는 방 이름만 클릭 (멤버수 접미사 예외 허용)
   → 1차 탐색 실패 시 1초 추가 대기 후 재시도
   → 그래도 없으면 오류 반환 (절대 다른 방으로 진입하지 않음)
2. 이미지 먼저 발송 → 텍스트 순서로 변경
3. 메세지 줄바꿈 한 번에 붙여넣기
   → 전체 텍스트를 클립보드에 한 번에 올려 붙여넣기
   → 줄바꿈이 있어도 끊김 없이 전송
4. 여러 채팅방에 동시 전송 시 서로 충돌 방지
   → threading.Lock()으로 한 번에 한 방씩 순서대로 전송
=============================================================
"""

import os
import io
import re
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
#  좌표 설정 — GUI 가이드 탭에서 조정 가능
# ─────────────────────────────────────────────────────────
_COORD: dict = {
    # ① 채팅 탭 버튼
    "chat_tab_x_offset":    75,    # left + N px
    "chat_tab_y_ratio":    0.30,   # top + (height × N%)

    # ② 검색 버튼 (🔍)
    "search_x_from_right": 130,    # right - N px
    "search_y_from_top":    55,    # top + N px

    # ③ 채팅 입력창 (탭 모드 — 사이드바+목록 합산 폭)
    "input_tab_offset":    330,    # 채팅룸 시작 x = left + N px
    "input_y_from_bottom":  50,    # bottom - N px
}


def update_coord(key: str, value) -> None:
    """GUI 좌표 가이드 탭에서 호출 — 런타임 좌표 업데이트."""
    if key in _COORD:
        _COORD[key] = value
        logger.info(f"좌표 업데이트: {key} = {value}")


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
# 카카오톡 UI 버튼/힌트 텍스트 (채팅방이 아닌 UI 요소)
_UI_HINTS = frozenset({
    "이스케이프", "ESC", "Escape", "닫기", "취소", "검색",
    "돌아가기", "뒤로", "더보기", "전체", "채팅",
})

# 멤버 수 접미사 패턴: "(5)", "(12명)" 등
_MEMBER_SUFFIX_RE = re.compile(r'^\s*\(\d+명?\)\s*$')


def _is_name_match(candidate: str, target: str) -> bool:
    """
    채팅방 이름 일치 여부 판단.

    [허용 규칙]
    1. 완전 일치: candidate == target
    2. 멤버 수 접미사 허용: target 뒤에 "(12명)" 또는 "(5)" 형태만 붙은 경우

    [차단 규칙]
    - 시작만 같아도 접미사가 멤버수 패턴이 아니면 다른 방으로 간주 → False
    """
    c = candidate.strip()
    t = target.strip()
    if c == t:
        return True
    if c.startswith(t):
        suffix = c[len(t):]
        if _MEMBER_SUFFIX_RE.match(suffix):
            return True
    return False


def _select_room_from_results(app: "Application", room_name: str) -> bool:
    """
    검색 결과에서 room_name과 정확히 일치하는 채팅방만 클릭한다.

    [안전 정책 — 절대 다른 방으로 진입 금지]
    - 완전 일치 또는 멤버 수 접미사만 허용 (예: "팀방 (5)" → "팀방" 검색 시 허용)
    - 단순 포함 일치(contains)는 사용하지 않음
    - 탐색 실패 시 False 반환 → 호출부에서 재시도 후 오류 처리

    [탐색 순서]
    1. ListItem 컨트롤에서 이름 일치 탐색
    2. 전체 컨트롤 대상 이름 일치 탐색 (카카오톡 Custom 컨트롤 대비)
    """

    def _is_room(text: str) -> bool:
        return bool(text) and text not in _UI_HINTS and len(text) > 1

    def _try_click_matched(controls_texts):
        """(ctrl, text) 목록에서 일치하는 첫 번째 항목 클릭."""
        for ctrl, t in controls_texts:
            if _is_room(t) and _is_name_match(t, room_name):
                try:
                    ctrl.click_input()
                    logger.info(f"✅ 채팅방 클릭 성공: '{t}' (검색어: '{room_name}')")
                    return True
                except Exception as e:
                    logger.debug(f"클릭 시도 실패 ({t}): {e}")
        return False

    try:
        chat_win = app.top_window()

        # ── 방법 1: ListItem 컨트롤 탐색 ─────────────────────────
        list_items = chat_win.descendants(control_type="ListItem")
        items_with_text = []
        for item in list_items:
            try:
                t = item.window_text().strip()
                if t:
                    items_with_text.append((item, t))
            except Exception:
                pass

        logger.info(f"검색 결과(ListItem): {[t for _, t in items_with_text]}")

        if _try_click_matched(items_with_text):
            return True

        # ── 방법 2: 전체 컨트롤 탐색 (Custom 컨트롤 대비) ────────
        logger.info("ListItem 탐색 실패 → 전체 컨트롤 탐색 시도")
        all_ctrls = chat_win.descendants()
        broad = []
        for ctrl in all_ctrls:
            try:
                ctype = ctrl.element_info.control_type
                if ctype in ("Edit", "Document"):   # 검색 입력창 제외
                    continue
                t = ctrl.window_text().strip()
                if t:
                    broad.append((ctrl, t))
            except Exception:
                pass

        logger.info(f"전체 탐색 후보: {[t for _, t in broad if _is_room(t)][:8]}")
        if _try_click_matched(broad):
            return True

        logger.warning(f"'{room_name}' 일치 항목 없음 (탐색 완료)")
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
    chat_tab_x = left + _COORD["chat_tab_x_offset"]
    chat_tab_y = top  + int(height * _COORD["chat_tab_y_ratio"])
    _mouse_click(chat_tab_x, chat_tab_y)
    logger.info(f"①채팅 탭 클릭: ({chat_tab_x}, {chat_tab_y})")
    time.sleep(0.5)

    # ── Step 2: 🔍 검색 버튼 클릭 ────────────────────────────
    search_x = right - _COORD["search_x_from_right"]
    search_y = top   + _COORD["search_y_from_top"]
    _mouse_click(search_x, search_y)
    logger.info(f"②검색 버튼 클릭: ({search_x}, {search_y})")
    time.sleep(0.5)

    # ── Step 3: 채팅방 이름 입력 ─────────────────────────────
    # 검색창이 열린 상태에서 클립보드로 붙여넣기 (한글 안전)
    _clip_text(room_name)
    send_keys("^v")
    time.sleep(1.5)   # 검색 결과 로딩 대기 (충분히)
    logger.info(f"'{room_name}' 검색 입력 완료")

    # ── Step 4: 정확한 채팅방 선택 (최대 2회 시도) ─────────
    # 완전 일치만 허용 — 절대 다른 방으로 진입하지 않음
    selected = _select_room_from_results(app, room_name)
    if not selected:
        logger.info("1차 탐색 실패 → 1.5초 추가 대기 후 재시도")
        time.sleep(1.5)
        selected = _select_room_from_results(app, room_name)

    if not selected:
        raise RuntimeError(
            f"'{room_name}' 채팅방을 검색 결과에서 찾을 수 없습니다.\n"
            f"채팅방 이름이 카카오톡과 정확히 일치하는지 확인해주세요.\n"
            f"(공백·특수문자 포함)"
        )
    time.sleep(1.0)

    # ── Step 5: 팝업 창이면 재연결 ────────────────────────────
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg_hwnd)
    logger.info(f"열린 창: '{fg_title}' (HWND={fg_hwnd})")

    if fg_hwnd and fg_hwnd != main_hwnd:
        try:
            chat_app = Application(backend="uia").connect(handle=fg_hwnd)
            logger.info("채팅 팝업 창 연결 성공")
            return chat_app, fg_hwnd, True   # (app, hwnd, is_popup=True)
        except Exception as e:
            logger.warning(f"팝업 연결 실패: {e} → 메인 창 사용")

    # 탭 모드: 채팅방이 메인 창 안에 열림
    logger.info("탭 모드 채팅방 열림 → 메인 창 사용")
    return app, main_hwnd, False   # (app, hwnd, is_popup=False)


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅 입력창 좌표 클릭
# ─────────────────────────────────────────────────────────
def _click_chat_input_area(chat_hwnd: int, is_popup: bool):
    """
    채팅 입력창 영역을 좌표로 직접 클릭한다.

    [카카오톡 창 구조]
    팝업 모드: 창 전체 = 채팅방  →  수평 중앙 하단
    탭 모드:   사이드바(~60px) + 채팅목록(~270px) + 채팅방(나머지)
               → 전체 너비의 60% 지점 하단

    입력창 y 위치 = bottom - 50px  (카카오톡 입력창 높이 약 40~55px)
    """
    left, top, right, bottom = win32gui.GetWindowRect(chat_hwnd)
    width  = right - left
    height = bottom - top

    if is_popup:
        # 팝업 모드: 사이드바·목록 없음 → 정중앙
        click_x = left + width // 2
    else:
        # 탭 모드: 사이드바+목록 폭(_COORD) 제외 후 나머지 중앙
        offset = _COORD["input_tab_offset"]
        click_x = left + offset + (width - offset) // 2

    click_y = bottom - _COORD["input_y_from_bottom"]

    # 창 포그라운드 활성화
    try:
        win32gui.SetForegroundWindow(chat_hwnd)
        time.sleep(0.25)
    except Exception as e:
        logger.debug(f"SetForegroundWindow 실패(무시): {e}")

    _mouse_click(click_x, click_y)
    logger.info(f"입력창 좌표 클릭: ({click_x}, {click_y})  팝업={is_popup}")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 텍스트 전송
# ─────────────────────────────────────────────────────────
def _send_text(app: "Application", message: str,
               chat_hwnd: int = 0, is_popup: bool = False):
    """
    채팅 입력창에 텍스트를 붙여넣고 전송한다.

    [입력창 클릭 방식]
    좌표 기반 클릭(주)을 먼저 시도하고,
    hwnd가 없을 때만 pywinauto 컨트롤 탐색(부)으로 fallback.

    [줄바꿈 처리]
    전체 메세지를 클립보드에 한 번에 올려 Ctrl+V로 붙여넣기.
    카카오톡은 붙여넣기 시 \\n을 줄바꿈으로 처리하므로 끊김 없음.
    """
    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.3)

    # ── 입력창 클릭: 좌표 우선 ─────────────────────────────
    if chat_hwnd:
        _click_chat_input_area(chat_hwnd, is_popup)
        time.sleep(0.3)
    else:
        # fallback: pywinauto 컨트롤 탐색
        try:
            for ctrl_type in ["Edit", "Document", "RichEdit20W", "RICHEDIT50W"]:
                candidates = chat_win.descendants(control_type=ctrl_type)
                enabled = [c for c in candidates if c.is_enabled()]
                if enabled:
                    enabled[-1].click_input()
                    time.sleep(0.2)
                    break
        except Exception as e:
            logger.warning(f"컨트롤 클릭 실패: {e}")

    # ── 메세지 붙여넣기 및 전송 ────────────────────────────
    _clip_text(message)
    send_keys("^v")
    time.sleep(0.4)
    send_keys("{ENTER}")
    time.sleep(0.5)
    logger.info("텍스트 전송 완료")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 이미지 전송
# ─────────────────────────────────────────────────────────
def _send_image(app: "Application", image_path: str,
                chat_hwnd: int = 0, is_popup: bool = False):
    """이미지를 클립보드 경유로 채팅창에 전송한다."""
    if not os.path.isfile(image_path):
        logger.warning(f"이미지 파일 없음, 건너뜀: {image_path}")
        return

    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.3)

    # ── 이미지를 클립보드에 복사 ───────────────────────────
    _clip_image(image_path)
    time.sleep(0.4)

    # ── 입력창 클릭: 좌표 우선 ─────────────────────────────
    if chat_hwnd:
        _click_chat_input_area(chat_hwnd, is_popup)
        time.sleep(0.3)
    else:
        try:
            for ctrl_type in ["Edit", "Document", "RichEdit20W", "RICHEDIT50W"]:
                candidates = chat_win.descendants(control_type=ctrl_type)
                enabled = [c for c in candidates if c.is_enabled()]
                if enabled:
                    enabled[-1].click_input()
                    time.sleep(0.2)
                    break
        except Exception as e:
            logger.warning(f"컨트롤 클릭 실패: {e}")

    # ── 붙여넣기 및 전송 ────────────────────────────────────
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

            # 2. 채팅방 검색·열기 → (app, hwnd, is_popup) 반환
            chat_app, chat_hwnd, is_popup = _open_chat_room(app, room_name, main_hwnd)
            time.sleep(0.8)   # 채팅방 완전 로딩 대기

            # 3. 이미지 먼저 전송 (이미지가 있을 때)
            if image_path:
                _send_image(chat_app, image_path, chat_hwnd, is_popup)
                time.sleep(0.5)

            # 4. 텍스트 전송
            if message and message.strip():
                _send_text(chat_app, message, chat_hwnd, is_popup)

            logger.info(f"✅ 발송 완료 → 채팅방: '{room_name}'")
            return {"success": True, "error": ""}

        except RuntimeError as e:
            logger.error(f"❌ 발송 실패 [{room_name}]: {e}")
            return {"success": False, "error": str(e)}

        except Exception as e:
            logger.error(f"❌ 예상치 못한 오류 [{room_name}]: {e}", exc_info=True)
            return {"success": False, "error": f"예상치 못한 오류: {e}"}
