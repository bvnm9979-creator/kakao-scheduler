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
import json
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

    # ③ 채팅 입력창
    # [중요] 카카오톡 하단 구조:
    #   bottom - 0~5px   : 창 테두리
    #   bottom - 5~45px  : 텍스트 입력창  ← 여기 클릭해야 함
    #   bottom - 45~80px : 이모티콘/첨부 버튼 행  ← 이전 50px가 여기였음!
    "input_tab_offset":    330,    # 탭 모드 채팅룸 시작 x = left + N px
    "input_y_from_bottom":  25,    # bottom - N px  (기본 25 = 입력창 텍스트 영역)
}


def update_coord(key: str, value) -> None:
    """GUI 좌표 가이드 탭에서 호출 — 런타임 좌표 업데이트."""
    if key in _COORD:
        _COORD[key] = value
        logger.info(f"좌표 업데이트: {key} = {value}")


# ─────────────────────────────────────────────────────────
#  캘리브레이션 — 입력창 절대 좌표 저장/로드
# ─────────────────────────────────────────────────────────
# [원리] 화면을 반반으로 고정 분할한 상태에서 사용자가 직접
#        마우스를 입력창에 올려놓으면 그 좌표를 저장.
#        이후 자동 발송 시 저장된 좌표를 정확히 클릭.
#        → offset 계산 방식보다 훨씬 신뢰도 높음.

_CALIBRATION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "kakao_coords.json"
)


def load_calibrated_coords() -> tuple:
    """
    저장된 입력창 절대 좌표를 반환한다.
    캘리브레이션이 완료되지 않았거나 파일이 없으면 (0, 0) 반환.
    """
    try:
        with open(_CALIBRATION_FILE, encoding="utf-8") as f:
            d = json.load(f)
        x = int(d.get("input_abs_x", 0))
        y = int(d.get("input_abs_y", 0))
        if x > 0 and y > 0:
            logger.info(f"캘리브레이션 좌표 로드: ({x}, {y})")
            return x, y
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"캘리브레이션 파일 읽기 실패: {e}")
    return 0, 0


def save_calibrated_coords(x: int, y: int) -> None:
    """입력창 절대 좌표를 파일에 저장한다 (main.py에서 호출)."""
    try:
        data = {
            "input_abs_x": x,
            "input_abs_y": y,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "카카오톡 채팅 입력창의 절대 화면 좌표",
        }
        with open(_CALIBRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"캘리브레이션 좌표 저장 완료: ({x}, {y})")
    except Exception as e:
        logger.error(f"캘리브레이션 저장 실패: {e}")


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
#  내부 함수 — 채팅방 열기 (목록 직접 탐색 방식 — 검색 없음)
# ─────────────────────────────────────────────────────────
def _open_chat_room(app: "Application", room_name: str, main_hwnd: int):
    """
    카카오톡 채팅 목록에서 방을 직접 찾아 클릭한다.

    [이전 방식의 문제]
    검색창 → 검색 결과 클릭 → 채팅방 열림 → 입력창 포커스 ← 여기서 실패

    [새 방식: 검색 완전 제거]
    채팅 탭 확인 → 채팅 목록 ListItem 직접 탐색 → 방 클릭
    → 카카오톡이 자동으로 입력창에 포커스 (사람이 쓸 때와 동일)
    → 별도 입력창 클릭 불필요!

    [채팅 탭 클릭은 왜 하나?]
    친구탭/더보기 등 다른 탭이 열려있을 수 있어서
    채팅 탭 아이콘만 좌표로 클릭 (이것만 텍스트 없는 아이콘)
    """
    main_win = app.top_window()
    main_win.set_focus()
    time.sleep(0.4)

    left, top, right, bottom = win32gui.GetWindowRect(main_hwnd)
    height = bottom - top

    # ── Step 1: 채팅 탭 아이콘 클릭 (유일하게 좌표 필요) ─────
    chat_tab_x = left + _COORD["chat_tab_x_offset"]
    chat_tab_y = top  + int(height * _COORD["chat_tab_y_ratio"])
    _mouse_click(chat_tab_x, chat_tab_y)
    logger.info(f"①채팅 탭 클릭: ({chat_tab_x}, {chat_tab_y})")
    time.sleep(0.6)

    # ── Step 2: 채팅 목록에서 방 직접 탐색 ──────────────────
    # 검색 UI 없이 현재 보이는 채팅 목록 ListItem을 직접 탐색
    chat_win = app.top_window()

    def _collect_rooms():
        items = []
        try:
            for item in chat_win.descendants(control_type="ListItem"):
                try:
                    t = item.window_text().strip()
                    if t and t not in _UI_HINTS and len(t) > 1:
                        items.append((item, t))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"ListItem 탐색 중 오류: {e}")
        return items

    candidates = _collect_rooms()
    logger.info(f"채팅 목록 발견 ({len(candidates)}개): "
                f"{[t for _, t in candidates[:8]]}")

    # ── Step 3: 이름 일치하는 방 클릭 ────────────────────────
    for item, text in candidates:
        if _is_name_match(text, room_name):
            try:
                item.click_input()
                logger.info(f"✅ 채팅방 클릭 성공: '{text}'")
                time.sleep(1.2)   # 채팅방 열림 대기

                # 팝업 vs 탭 모드 판별
                fg_hwnd = win32gui.GetForegroundWindow()
                if fg_hwnd and fg_hwnd != main_hwnd:
                    try:
                        chat_app = Application(backend="uia").connect(handle=fg_hwnd)
                        logger.info("팝업 창 모드")
                        return chat_app, fg_hwnd, True
                    except Exception as e:
                        logger.warning(f"팝업 연결 실패: {e}")

                logger.info("탭 모드")
                return app, main_hwnd, False

            except Exception as e:
                logger.warning(f"방 클릭 실패 ({text}): {e}")

    # ── Step 4: 목록에서 못 찾으면 오류 ──────────────────────
    raise RuntimeError(
        f"'{room_name}' 채팅방을 채팅 목록에서 찾을 수 없습니다.\n\n"
        f"확인사항:\n"
        f"  • 카카오톡이 채팅 탭(💬)을 보여주고 있는지\n"
        f"  • 채팅방 이름이 정확히 일치하는지 (공백·특수문자 포함)\n"
        f"  • 해당 채팅방이 목록 상단에 있는지 (스크롤 내려야 보이면 직접 올려놓기)\n\n"
        f"현재 목록: {[t for _, t in candidates[:5]]}"
    )


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅 입력창 좌표 클릭
# ─────────────────────────────────────────────────────────
def _click_chat_input_area(chat_hwnd: int, is_popup: bool):
    """
    채팅 입력창 영역을 클릭한다.

    [클릭 방식 우선순위]
    1순위: 캘리브레이션 절대 좌표 (사용자가 직접 지정, 가장 정확)
    2순위: offset 계산 방식 (캘리브레이션 미완료 시 폴백)

    [캘리브레이션이란?]
    사용자가 '🎯 입력창 위치 지정' 버튼으로 마우스를 입력창에
    직접 올려놓으면 그 절대 좌표를 저장. 이후 항상 그 좌표를 클릭.
    화면을 반반 고정 분할해 두면 영구적으로 유효.
    """
    # ── 1순위: 캘리브레이션 절대 좌표 ───────────────────────
    cal_x, cal_y = load_calibrated_coords()
    if cal_x and cal_y:
        try:
            win32gui.SetForegroundWindow(chat_hwnd)
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"SetForegroundWindow 실패(무시): {e}")
        _mouse_click(cal_x, cal_y)
        logger.info(f"✅ 캘리브레이션 좌표로 입력창 클릭: ({cal_x}, {cal_y})")
        time.sleep(0.3)
        return

    # ── 2순위: offset 계산 폴백 ──────────────────────────────
    logger.warning("캘리브레이션 미완료 → offset 계산 방식 사용 (정확도 낮음)")
    left, top, right, bottom = win32gui.GetWindowRect(chat_hwnd)
    width  = right - left

    # X 계산
    if is_popup:
        click_x = left + width // 2
    else:
        offset  = _COORD["input_tab_offset"]
        click_x = left + offset + (width - offset) // 2

    # 창 포그라운드 활성화
    try:
        win32gui.SetForegroundWindow(chat_hwnd)
        time.sleep(0.3)
    except Exception as e:
        logger.debug(f"SetForegroundWindow 실패(무시): {e}")

    # Y: 여러 위치 순서대로 클릭 (하나라도 맞으면 포커스 획득)
    primary_y  = _COORD["input_y_from_bottom"]
    y_attempts = sorted({primary_y, 20, 30}, reverse=True)

    for y_off in y_attempts:
        cy = bottom - y_off
        _mouse_click(click_x, cy)
        logger.info(f"입력창 클릭(offset): ({click_x}, {cy})  y_off={y_off}")
        time.sleep(0.15)

    time.sleep(0.2)


# ─────────────────────────────────────────────────────────
#  내부 함수 — 텍스트 전송
# ─────────────────────────────────────────────────────────
def _send_text(app: "Application", message: str,
               chat_hwnd: int = 0, is_popup: bool = False):
    """
    채팅 입력창에 텍스트를 붙여넣고 전송한다.

    [입력창 포커스 전략 — 클릭 없음!]
    채팅 목록에서 방을 직접 클릭해서 열면
    카카오톡이 자동으로 입력창에 포커스를 주므로
    별도 입력창 클릭 없이 바로 Ctrl+V 가능.

    [줄바꿈 처리]
    전체 메세지를 클립보드에 한 번에 올려 Ctrl+V로 붙여넣기.
    """
    # KakaoTalk 창을 전면으로 (포커스 유지)
    try:
        win32gui.SetForegroundWindow(chat_hwnd)
        time.sleep(0.4)
    except Exception as e:
        logger.debug(f"SetForegroundWindow 실패(무시): {e}")

    # ── 클립보드 → 붙여넣기 → 전송 ────────────────────────
    _clip_text(message)
    time.sleep(0.2)
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
    """
    이미지를 클립보드 경유로 채팅창에 전송한다.
    입력창 포커스는 채팅방 진입 시 카카오톡이 자동 부여하므로
    별도 클릭 없이 바로 붙여넣기.
    """
    if not os.path.isfile(image_path):
        logger.warning(f"이미지 파일 없음, 건너뜀: {image_path}")
        return

    # KakaoTalk 창 전면 유지
    try:
        win32gui.SetForegroundWindow(chat_hwnd)
        time.sleep(0.4)
    except Exception as e:
        logger.debug(f"SetForegroundWindow 실패(무시): {e}")

    # ── 이미지 클립보드 복사 → 붙여넣기 → 전송 ──────────
    _clip_image(image_path)
    time.sleep(0.4)
    send_keys("^v")
    time.sleep(0.8)
    send_keys("{ENTER}")
    time.sleep(0.6)
    logger.info(f"이미지 전송 완료: {os.path.basename(image_path)}")


# ─────────────────────────────────────────────────────────
#  공개 API — 전체 채팅방 발송 함수
# ─────────────────────────────────────────────────────────
def send_to_all_rooms(message: str, image_path: str = None,
                      progress_cb=None) -> dict:
    """
    채팅 목록에 있는 모든 채팅방에 순서대로 메시지를 전송한다.

    [동작 순서]
    1. 채팅 탭 클릭 (사이드바 아이콘)
    2. 채팅 목록의 ListItem 전부 수집
    3. 각 방: 클릭 → 입력창 자동 포커스 → 붙여넣기 → 전송
    4. 팝업이면 닫기, 탭이면 다음 방 클릭 시 자동 교체

    [progress_cb]
    진행 상황을 UI에 알리는 콜백: progress_cb(current, total, room_name)
    """
    ok, reason = check_available()
    if not ok:
        return {"success": False, "error": reason, "count": 0, "total": 0}

    with _send_lock:
        try:
            app, main_hwnd = _activate_kakao()
            time.sleep(0.5)

            # ── 채팅 탭 클릭 ────────────────────────────────
            left, top, right, bottom = win32gui.GetWindowRect(main_hwnd)
            height = bottom - top
            chat_tab_x = left + _COORD["chat_tab_x_offset"]
            chat_tab_y = top + int(height * _COORD["chat_tab_y_ratio"])
            _mouse_click(chat_tab_x, chat_tab_y)
            time.sleep(0.8)

            # ── 채팅방 이름만 수집 (참조 X — UI 갱신 후 참조 무효 방지) ──
            chat_win = app.top_window()
            room_names = []
            try:
                for item in chat_win.descendants(control_type="ListItem"):
                    try:
                        t = item.window_text().strip()
                        if t and t not in _UI_HINTS and len(t) > 1:
                            room_names.append(t)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"목록 탐색 오류: {e}")

            if not room_names:
                return {
                    "success": False,
                    "error": "채팅방 목록을 찾을 수 없습니다.\n"
                             "카카오톡 채팅 탭이 열려 있는지 확인해주세요.",
                    "count": 0, "total": 0,
                }

            total = len(room_names)
            logger.info(f"전체 채팅방 {total}개 발송 시작: {room_names}")
            success_count = 0
            fail_rooms = []

            # ── 각 채팅방에 순서대로 발송 ────────────────────
            for i, room_name in enumerate(room_names):
                logger.info(f"[{i+1}/{total}] '{room_name}' 발송 중...")
                if progress_cb:
                    try:
                        progress_cb(i + 1, total, room_name)
                    except Exception:
                        pass

                try:
                    # ① 메인 창 활성화 + 채팅 탭 재클릭 (매번 초기 상태로 복귀)
                    try:
                        win32gui.SetForegroundWindow(main_hwnd)
                    except Exception:
                        pass
                    time.sleep(0.3)
                    _mouse_click(chat_tab_x, chat_tab_y)
                    time.sleep(0.6)

                    # ② 목록 새로 수집 → 이름 일치하는 방 클릭
                    chat_win = app.top_window()
                    clicked = False
                    for item in chat_win.descendants(control_type="ListItem"):
                        try:
                            t = item.window_text().strip()
                            if not t:
                                continue
                            # 완전 일치 또는 멤버수 접미사 허용
                            if t == room_name or (
                                t.startswith(room_name) and
                                _MEMBER_SUFFIX_RE.match(t[len(room_name):])
                            ):
                                item.click_input()
                                clicked = True
                                break
                        except Exception:
                            pass

                    if not clicked:
                        logger.warning(f"[{i+1}/{total}] '{room_name}' 찾기 실패, 건너뜀")
                        fail_rooms.append(room_name)
                        continue

                    time.sleep(1.0)

                    # ③ 팝업 vs 탭 모드 판별
                    fg_hwnd  = win32gui.GetForegroundWindow()
                    is_popup = bool(fg_hwnd and fg_hwnd != main_hwnd)
                    chat_hwnd = fg_hwnd if is_popup else main_hwnd

                    # ④ 입력창 클릭 — 자동 포커스 믿지 않고 직접 클릭
                    _click_chat_input_area(chat_hwnd, is_popup)
                    time.sleep(0.3)

                    # ⑤ 이미지 전송
                    if image_path and os.path.isfile(image_path):
                        _clip_image(image_path)
                        time.sleep(0.4)
                        send_keys("^v")
                        time.sleep(0.8)
                        send_keys("{ENTER}")
                        time.sleep(0.5)

                    # ⑥ 텍스트 전송
                    if message and message.strip():
                        _clip_text(message)
                        time.sleep(0.2)
                        send_keys("^v")
                        time.sleep(0.4)
                        send_keys("{ENTER}")
                        time.sleep(0.5)

                    # ⑦ 팝업이면 닫기
                    if is_popup:
                        time.sleep(0.3)
                        try:
                            win32gui.PostMessage(chat_hwnd, win32con.WM_CLOSE, 0, 0)
                            time.sleep(0.6)
                        except Exception:
                            pass

                    success_count += 1
                    logger.info(f"✅ [{i+1}/{total}] '{room_name}' 발송 완료")

                except Exception as e:
                    logger.error(f"❌ [{i+1}/{total}] '{room_name}' 실패: {e}")
                    fail_rooms.append(room_name)
                    continue

            err_msg = (f"{len(fail_rooms)}개 실패: {', '.join(fail_rooms[:3])}"
                       if fail_rooms else "")
            return {
                "success": success_count > 0,
                "count":   success_count,
                "total":   total,
                "error":   err_msg,
            }

        except Exception as e:
            logger.error(f"전체 발송 오류: {e}", exc_info=True)
            return {"success": False, "error": str(e), "count": 0, "total": 0}


# ─────────────────────────────────────────────────────────
#  공개 API — 특정 채팅방 발송 함수 (하위 호환 유지)
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

            # 1. 카카오톡 메인 창 활성화
            app, main_hwnd = _activate_kakao()
            time.sleep(0.5)

            # 2. 채팅 목록에서 직접 방 클릭 → (app, hwnd, is_popup) 반환
            #    [핵심] 검색 UI 없이 목록 ListItem 직접 클릭
            #    → 카카오톡이 자동으로 입력창 포커스 부여
            chat_app, chat_hwnd, is_popup = _open_chat_room(app, room_name, main_hwnd)
            time.sleep(1.0)   # 채팅방 열림 대기 (검색보다 빠름)

            # 3. 이미지 먼저 전송
            if image_path:
                _send_image(chat_app, image_path, chat_hwnd, is_popup)
                time.sleep(0.5)

            # 4. 텍스트 전송
            if message and message.strip():
                _send_text(chat_app, message, chat_hwnd, is_popup)

            # 5. 팝업 창이면 전송 후 닫기 → 다음 방으로 자연스럽게 이동
            #    탭 모드는 다음 방 클릭 시 자동 교체되므로 닫을 필요 없음
            if is_popup:
                time.sleep(0.4)
                try:
                    win32gui.PostMessage(chat_hwnd, win32con.WM_CLOSE, 0, 0)
                    time.sleep(0.5)
                    logger.info("팝업 채팅창 닫기 완료")
                except Exception as e:
                    logger.debug(f"팝업 닫기 실패(무시): {e}")

            logger.info(f"✅ 발송 완료 → 채팅방: '{room_name}'")
            return {"success": True, "error": ""}

        except RuntimeError as e:
            logger.error(f"❌ 발송 실패 [{room_name}]: {e}")
            return {"success": False, "error": str(e)}

        except Exception as e:
            logger.error(f"❌ 예상치 못한 오류 [{room_name}]: {e}", exc_info=True)
            return {"success": False, "error": f"예상치 못한 오류: {e}"}
