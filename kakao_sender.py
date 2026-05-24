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
#  내부 함수 — 채팅방 열기
# ─────────────────────────────────────────────────────────
def _open_chat_room(app: "Application", room_name: str, main_hwnd: int) -> "Application":
    """
    카카오톡에서 채팅방을 검색해서 연다.

    [수정된 동작]
    1. Ctrl+F 검색 → 채팅방 이름 입력 → 클릭으로 채팅방 열기
    2. 채팅방이 새 팝업 창으로 열리면 → 해당 창에 다시 연결
       (기존 코드는 메인 창에만 연결되어 검색란에 메세지가 입력되던 문제 수정)
    3. 채팅방이 탭으로 열리면 → Escape로 검색 UI 닫고 입력 준비

    [채팅방 선택 우선순위]
    1순위: 이름이 정확히 일치 (1:1 채팅 등)
    2순위: 이름이 포함된 방 (그룹채팅 등)
    3순위: Enter 키 fallback
    """
    main_win = app.top_window()
    main_win.set_focus()
    time.sleep(0.5)

    # ── 검색창 열기 (Ctrl+F) ──
    send_keys("^f")
    time.sleep(0.8)

    # ── 채팅방 이름 입력 ──
    send_keys("^a")          # 기존 검색어 지우기
    time.sleep(0.1)
    _clip_text(room_name)
    send_keys("^v")
    time.sleep(1.2)          # 검색 결과 로딩 대기

    # ── 검색 결과에서 채팅방 선택 ──
    try:
        all_items = main_win.descendants(control_type="ListItem")

        # 1순위: 정확히 일치하는 방
        exact_match = None
        for item in all_items:
            try:
                if item.window_text().strip() == room_name:
                    exact_match = item
                    break
            except Exception:
                continue

        if exact_match:
            exact_match.double_click_input()
            logger.info(f"채팅방 '{room_name}' 정확 일치 클릭")
        else:
            # 2순위: 이름이 포함된 방
            partial_match = None
            for item in all_items:
                try:
                    if room_name in item.window_text():
                        partial_match = item
                        break
                except Exception:
                    continue

            if partial_match:
                partial_match.double_click_input()
                logger.warning(
                    f"'{room_name}' 정확 일치 없음 → "
                    f"포함된 방 선택: '{partial_match.window_text().strip()}'"
                )
            else:
                # 3순위: Enter 키
                logger.warning("ListItem 없음 → Enter 키 fallback")
                send_keys("{ENTER}")

    except Exception as e:
        logger.warning(f"채팅방 클릭 예외: {e} → Enter 키 fallback")
        send_keys("{ENTER}")

    # ── 채팅방이 열릴 때까지 대기 ──
    time.sleep(1.2)

    # ── [핵심 수정] 실제로 열린 창에 연결 ──────────────────────
    # 카카오톡은 채팅방을 별도 팝업 창으로 열 수 있음.
    # 그 경우 기존 app 객체(메인 창)로는 채팅 입력창을 찾을 수 없어
    # 검색란에 메세지가 입력되는 버그 발생.
    # → GetForegroundWindow()로 현재 활성 창을 감지해 다시 연결.
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg_hwnd)
    logger.info(f"채팅 열린 후 활성 창: '{fg_title}' (HWND={fg_hwnd})")

    if fg_hwnd and fg_hwnd != main_hwnd:
        # 새 팝업 창으로 열린 경우
        try:
            chat_app = Application(backend="uia").connect(handle=fg_hwnd)
            logger.info(f"채팅 팝업 창 연결 성공")
            return chat_app
        except Exception as e:
            logger.warning(f"채팅 팝업 연결 실패: {e} → 메인 창 사용")

    # 탭으로 열린 경우 → Escape로 검색 UI 닫기
    send_keys("{ESCAPE}")
    time.sleep(0.4)
    logger.info("탭 모드: 검색 UI 닫음")
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
