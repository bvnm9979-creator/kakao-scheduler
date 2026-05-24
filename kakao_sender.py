"""
=============================================================
  kakao_sender.py  —  카카오톡 PC버전 자동 제어 모듈
=============================================================
[중요] 이 모듈은 Windows 전용 라이브러리(pywinauto, pywin32)를 사용합니다.
macOS에서는 import 자체는 성공하지만, 실제 함수 호출은 비활성화됩니다.
→ IS_WINDOWS 변수로 플랫폼을 구분하고, Windows 전용 코드는
  `if IS_WINDOWS:` 블록 안에만 배치하여 맥에서도 import 에러가 없도록 설계했습니다.

[카카오톡 PC버전 제어 흐름]
  1. win32gui로 "카카오톡" 창 핸들(HWND) 탐색
  2. pywinauto로 창 활성화 및 포그라운드 전환
  3. Ctrl+F 단축키로 통합 검색창 열기
  4. pyperclip으로 채팅방 이름을 클립보드 복사 → Ctrl+V 붙여넣기 (한글 깨짐 방지)
  5. 검색 결과에서 채팅방 항목 클릭 (pywinauto ListItem 탐색)
  6. 채팅 입력창에 텍스트 클립보드 붙여넣기 + Enter 전송
  7. 이미지 전송: PIL → DIB 변환 → win32clipboard → Ctrl+V + Enter
=============================================================
"""

import os
import io
import sys
import time
import platform
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
#  플랫폼 감지 및 Windows 전용 라이브러리 조건부 임포트
#  맥(macOS)에서는 아래 블록을 건너뛰고 HAS_WIN_DEPS = False 로 설정
# ─────────────────────────────────────────────────────────
IS_WINDOWS: bool = platform.system() == "Windows"
HAS_WIN_DEPS: bool = False  # 기본값: 비활성화

if IS_WINDOWS:
    try:
        # GUI 자동화
        import pywinauto
        from pywinauto import Application
        from pywinauto.keyboard import send_keys
        from pywinauto.findwindows import ElementNotFoundError

        # 클립보드 (한글 안전 텍스트 입력)
        import pyperclip

        # Windows API 직접 접근
        import win32gui       # 창 핸들 탐색
        import win32con       # 창 상수 (SW_RESTORE 등)
        import win32clipboard  # 이미지 클립보드 복사

        # 이미지 처리
        from PIL import Image

        HAS_WIN_DEPS = True
        logger.info("Windows 자동화 라이브러리 로드 성공")

    except ImportError as e:
        # 패키지가 설치되지 않은 경우
        HAS_WIN_DEPS = False
        logger.warning(f"Windows 자동화 라이브러리 로드 실패: {e}\n"
                       f"→ requirements.txt 패키지를 설치해주세요.")
else:
    # macOS / Linux: import는 통과되지만 기능 비활성화
    logger.info("비-Windows 환경 감지: 카카오톡 자동화 기능이 비활성화됩니다.")


# ─────────────────────────────────────────────────────────
#  공개 API: 환경 확인
# ─────────────────────────────────────────────────────────
def check_available() -> tuple:
    """
    현재 환경에서 카카오톡 자동화가 가능한지 확인한다.
    반환: (가능여부: bool, 이유: str)
    """
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
    카카오톡 메인 창 핸들(HWND)을 탐색한다.

    [탐색 전략 — 3단계 Fallback]
    1. 정확한 창 제목으로 탐색 ("카카오톡", "KakaoTalk")
       → 가장 빠르고 안정적. 카카오톡 업데이트 후 창 이름이 바뀌면 실패할 수 있음.
    2. 부분 문자열 탐색 ("카카오", "kakao")
       → EnumWindows로 전체 창을 순회하여 이름에 키워드가 포함된 창을 찾음.
    3. 발견 못 하면 0 반환

    반환: HWND (int), 못 찾으면 0
    """
    # ── 단계 1: 정확한 제목 매칭 ──
    for title in ["카카오톡", "KakaoTalk"]:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            logger.debug(f"카카오톡 창 발견 (정확 매칭): HWND={hwnd}, 제목='{title}'")
            return hwnd

    # ── 단계 2: 부분 문자열 전체 창 탐색 ──
    found_hwnds = []

    def _enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if "카카오" in title or "kakao" in title.lower():
            found_hwnds.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)

    if found_hwnds:
        logger.debug(f"카카오톡 창 발견 (부분 매칭): HWND={found_hwnds[0]}")
        return found_hwnds[0]

    logger.error("카카오톡 창을 찾을 수 없음. 카카오톡이 실행 중인지 확인하세요.")
    return 0


def _activate_kakao() -> "Application":
    """
    카카오톡 창을 최소화 해제 + 포그라운드로 가져온 뒤
    pywinauto Application 객체를 반환한다.

    [주의] 화면 잠금(Lock Screen) 상태에서는 SetForegroundWindow가
    실패할 수 있다. 반드시 Windows 세션이 잠금 해제 상태여야 한다.
    """
    hwnd = _find_kakao_hwnd()
    if not hwnd:
        raise RuntimeError(
            "카카오톡 창을 찾을 수 없습니다.\n"
            "카카오톡 PC버전이 실행 중인지 확인해주세요."
        )

    # 최소화 상태(아이콘 상태) 복원
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)

    # 포그라운드 전환
    # (다른 앱이 포커스를 가지고 있으면 깜빡임이 발생할 수 있으나, 기능적으로는 정상)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        logger.warning(f"SetForegroundWindow 실패 (일반적으로 무시 가능): {e}")

    time.sleep(0.5)

    # pywinauto에 HWND로 연결 (UIA 백엔드: 현대적인 컨트롤 트리 탐색)
    app = Application(backend="uia").connect(handle=hwnd)
    return app


# ─────────────────────────────────────────────────────────
#  내부 함수 — 클립보드
# ─────────────────────────────────────────────────────────
def _clip_text(text: str):
    """
    텍스트를 클립보드에 복사한다.
    pyperclip을 사용하면 Windows에서 한글이 안전하게 처리된다.
    (send_keys로 한글을 직접 입력하면 깨지는 경우가 있음)
    """
    pyperclip.copy(text)


def _clip_image(image_path: str):
    """
    이미지 파일을 Windows 클립보드에 DIB(Device-Independent Bitmap) 형식으로 복사한다.

    [원리]
    - PIL로 이미지를 BMP 포맷으로 메모리에 인코딩
    - BMP 파일 헤더(14바이트)를 제거하면 DIB 데이터만 남음
    - win32clipboard.CF_DIB 형식으로 클립보드에 저장
    - 카카오톡 입력창에서 Ctrl+V를 누르면 이미지로 인식하여 전송 가능

    [주의] GIF 애니메이션은 첫 프레임만 복사됨.
    """
    img = Image.open(image_path).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="BMP")
    dib_data = buf.getvalue()[14:]  # BMP 파일 헤더 14바이트 제거 → DIB 포맷
    buf.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_data)
    finally:
        win32clipboard.CloseClipboard()

    logger.debug(f"이미지 클립보드 복사 완료: {os.path.basename(image_path)}")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅방 탐색 및 열기
# ─────────────────────────────────────────────────────────
def _open_chat_room(app: "Application", room_name: str):
    """
    카카오톡 메인 창에서 특정 채팅방을 검색하여 연다.

    [제어 흐름]
    1. Ctrl+F → 통합 검색창 열기
    2. 채팅방 이름 클립보드 붙여넣기 (한글 안전)
    3. pywinauto로 검색 결과 ListItem 탐색 → 방 이름 포함 항목 더블클릭
    4. 탐색 실패 시 → Enter 키로 첫 번째 결과 선택 (Fallback)

    [카카오톡 버전 주의]
    - 카카오톡 업데이트 시 검색 결과 컨트롤 구조가 바뀔 수 있음.
    - 그래도 클립보드+Enter Fallback이 대부분 작동함.
    """
    main_win = app.top_window()
    main_win.set_focus()
    time.sleep(0.3)

    # ── 검색창 열기 (Ctrl+F) ──
    send_keys("^f")
    time.sleep(0.7)

    # ── 채팅방 이름 입력 (클립보드 방식) ──
    _clip_text(room_name)
    send_keys("^v")
    time.sleep(1.0)  # 검색 결과 로딩 대기

    # ── 검색 결과에서 채팅방 항목 클릭 시도 ──
    try:
        # pywinauto UIA: ListItem 컨트롤 전체 탐색
        all_items = main_win.descendants(control_type="ListItem")

        # 방 이름이 포함된 항목 찾기
        matched = None
        for item in all_items:
            try:
                item_text = item.window_text()
                if room_name in item_text:
                    matched = item
                    break
            except Exception:
                continue

        if matched:
            matched.double_click_input()
            time.sleep(0.8)
            logger.info(f"채팅방 '{room_name}' 항목 클릭 성공")
            return

        # 정확히 매칭되는 항목 없으면 첫 번째 결과 클릭 시도
        if all_items:
            logger.warning(f"'{room_name}' 정확 매칭 실패. 첫 번째 결과 항목을 선택합니다.")
            all_items[0].double_click_input()
            time.sleep(0.8)
            return

        # 결과 항목 자체가 없으면 Enter로 선택 (Fallback)
        logger.warning("검색 결과 ListItem을 찾지 못했습니다. Enter 키로 대체합니다.")
        send_keys("{ENTER}")
        time.sleep(0.8)

    except Exception as e:
        # 예외 발생 시 Enter 키 Fallback
        logger.warning(f"채팅방 클릭 중 예외: {e}. Enter 키로 대체합니다.")
        send_keys("{ENTER}")
        time.sleep(0.8)


# ─────────────────────────────────────────────────────────
#  내부 함수 — 채팅창 입력 컨트롤 탐색
# ─────────────────────────────────────────────────────────
def _find_input_ctrl(app: "Application"):
    """
    현재 활성 채팅창에서 메시지 입력 컨트롤을 찾는다.

    카카오톡 버전/플러그인에 따라 컨트롤 타입이 다를 수 있어
    Edit → Document → RichEdit 순서로 fallback 시도한다.
    보통 마지막에 위치한 Edit 컨트롤이 입력창이다.
    """
    chat_win = app.top_window()

    for ctrl_type in ["Edit", "Document", "RichEdit20W", "RICHEDIT50W"]:
        try:
            candidates = chat_win.descendants(control_type=ctrl_type)
            if candidates:
                # 마지막 항목(입력창)을 반환
                return candidates[-1]
        except Exception:
            continue

    raise RuntimeError(
        "채팅 입력창 컨트롤을 찾을 수 없습니다.\n"
        "카카오톡 버전 업데이트 후 컨트롤 구조가 바뀌었을 수 있습니다."
    )


# ─────────────────────────────────────────────────────────
#  내부 함수 — 텍스트 전송
# ─────────────────────────────────────────────────────────
def _send_text(app: "Application", message: str):
    """
    채팅창에 텍스트 메시지를 입력하고 전송한다.

    [줄바꿈 처리]
    카카오톡에서 Enter는 전송이므로, 줄바꿈(\n)은
    Shift+Enter로 입력해야 한다.
    → 메시지를 '\n' 기준으로 split 후 한 줄씩 클립보드 붙여넣기,
      줄 사이에 Shift+Enter 삽입, 마지막 줄 후 Enter로 전송.

    [한글 처리]
    send_keys로 한글 직접 타이핑 시 IME 문제로 글자가 합쳐지거나 깨지는 현상 발생.
    pyperclip + Ctrl+V 클립보드 방식이 가장 안정적이다.
    """
    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.2)

    # 입력창 포커스
    try:
        ctrl = _find_input_ctrl(app)
        ctrl.click_input()
        time.sleep(0.2)
    except Exception as e:
        logger.warning(f"입력창 클릭 실패, Tab으로 대체: {e}")
        send_keys("{TAB}")
        time.sleep(0.2)

    # 줄 단위로 분리하여 입력
    lines = message.split("\n")
    for i, line in enumerate(lines):
        if line.strip():  # 빈 줄도 Shift+Enter로 처리 가능하나 내용 있는 줄만 붙여넣기
            _clip_text(line)
            send_keys("^v")
            time.sleep(0.15)

        # 마지막 줄이 아니면 Shift+Enter (줄바꿈, 전송 아님)
        if i < len(lines) - 1:
            send_keys("+{ENTER}")
            time.sleep(0.1)

    # 최종 Enter → 메시지 전송
    time.sleep(0.2)
    send_keys("{ENTER}")
    time.sleep(0.5)
    logger.info("텍스트 전송 완료")


# ─────────────────────────────────────────────────────────
#  내부 함수 — 이미지 전송
# ─────────────────────────────────────────────────────────
def _send_image(app: "Application", image_path: str):
    """
    이미지를 클립보드를 통해 채팅창에 전송한다.

    [원리]
    PIL로 이미지를 BMP→DIB 변환하여 win32clipboard에 저장 후,
    채팅 입력창에서 Ctrl+V 붙여넣기 → Enter 전송.

    [주의]
    - 전송 전 약간의 sleep이 필요 (카카오톡 이미지 미리보기 렌더링 대기)
    - 이미지 파일이 없으면 건너뜀
    """
    if not os.path.isfile(image_path):
        logger.warning(f"이미지 파일 없음, 전송 건너뜀: {image_path}")
        return

    chat_win = app.top_window()
    chat_win.set_focus()
    time.sleep(0.2)

    # 이미지를 클립보드에 복사
    _clip_image(image_path)
    time.sleep(0.3)

    # 입력창 포커스
    try:
        ctrl = _find_input_ctrl(app)
        ctrl.click_input()
        time.sleep(0.2)
    except Exception as e:
        logger.warning(f"입력창 클릭 실패, Tab으로 대체: {e}")
        send_keys("{TAB}")
        time.sleep(0.2)

    # 붙여넣기 후 이미지 미리보기 렌더링 대기
    send_keys("^v")
    time.sleep(0.8)

    # 전송
    send_keys("{ENTER}")
    time.sleep(0.6)
    logger.info(f"이미지 전송 완료: {os.path.basename(image_path)}")


# ─────────────────────────────────────────────────────────
#  공개 API — 메인 발송 함수
# ─────────────────────────────────────────────────────────
def send_to_room(room_name: str, message: str, image_path: str = None) -> dict:
    """
    카카오톡 특정 채팅방에 텍스트 메시지와/또는 이미지를 전송한다.

    Args:
        room_name  (str): 카카오톡 채팅방 이름 (정확히 일치해야 탐색 가능)
        message    (str): 전송할 텍스트 메시지. 줄바꿈 '\n' 포함 가능. 빈 문자열이면 생략.
        image_path (str): 전송할 이미지 파일의 절대 경로. None이면 생략.

    Returns:
        dict: {
            "success": bool,   # True: 성공, False: 실패
            "error":   str     # 실패 시 오류 메시지, 성공 시 ""
        }

    [운영 주의사항]
    - 카카오톡 PC버전이 실행 중이어야 한다.
    - 채팅방 이름이 정확히 일치해야 검색이 성공한다 (공백 포함).
    - Windows 세션이 잠금 해제 상태여야 한다 (화면 잠금 시 창 제어 불가).
    - 카카오톡 업데이트 후 창 구조가 바뀌면 일부 단계가 실패할 수 있다.
    """
    # ── 환경 사전 점검 ──
    ok, reason = check_available()
    if not ok:
        logger.warning(f"자동화 불가 환경: {reason}")
        return {"success": False, "error": reason}

    try:
        logger.info(f"▶ 발송 시작 → 채팅방: '{room_name}'")

        # 1. 카카오톡 창 활성화
        app = _activate_kakao()
        time.sleep(0.5)

        # 2. 채팅방 검색 및 열기
        _open_chat_room(app, room_name)
        time.sleep(0.6)

        # 3. 텍스트 전송 (메시지가 있을 때만)
        if message and message.strip():
            _send_text(app, message)
            time.sleep(0.5)

        # 4. 이미지 전송 (경로가 있을 때만)
        if image_path:
            _send_image(app, image_path)

        logger.info(f"✅ 발송 완료 → 채팅방: '{room_name}'")
        return {"success": True, "error": ""}

    except RuntimeError as e:
        # 예상된 오류 (창 없음, 입력창 없음 등)
        logger.error(f"❌ 발송 실패 [{room_name}]: {e}")
        return {"success": False, "error": str(e)}

    except Exception as e:
        # 예상치 못한 오류
        logger.error(f"❌ 예상치 못한 오류 [{room_name}]: {e}", exc_info=True)
        return {"success": False, "error": f"예상치 못한 오류: {e}"}
