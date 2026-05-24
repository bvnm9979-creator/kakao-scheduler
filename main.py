"""
=============================================================
  main.py  —  카카오톡 예약 메세지 자동 발송 프로그램
  GUI: CustomTkinter  |  자동화: kakao_sender.py
  개발: macOS  /  운영: Windows (.exe)
=============================================================
"""

import os
import sys
import uuid
import time
import platform
import logging
import threading
from datetime import datetime

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

# ── 카카오톡 자동화 모듈 (플랫폼 무관하게 import 가능)
try:
    from kakao_sender import send_to_room, check_available, update_coord
    KAKAO_MODULE_OK = True
except ImportError:
    KAKAO_MODULE_OK = False

    def send_to_room(room_name, message, image_path=None):
        return {"success": False, "error": "kakao_sender 모듈을 찾을 수 없습니다."}

    def check_available():
        return False, "kakao_sender 모듈 없음"

    def update_coord(key, value):
        pass


# ─────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# ── CustomTkinter 테마
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 카카오 노란색
KAKAO_YELLOW = "#FEE500"
KAKAO_BROWN  = "#3C1E1E"


# ─────────────────────────────────────────────
# 데이터 클래스: 예약 항목
# ─────────────────────────────────────────────
class ScheduleEntry:
    """
    하나의 '예약 설정'을 나타내는 인메모리 데이터 객체.
    프로그램을 끄면 사라진다 (저장 없음).
    """

    def __init__(self, times: list, rooms: list, message: str, image_path: str):
        self.id         = str(uuid.uuid4())
        self.times      = sorted(times)         # 발송 시간 목록 ["09:00", "13:00", ...]
        self.rooms      = rooms                  # 채팅방 이름 목록 ["팀방", "공지방", ...]
        self.message    = message                # 전송할 텍스트
        self.image_path = image_path             # 이미지 절대경로 (없으면 "")
        self.fired: set = set()                  # 이미 발송된 (time, room) 쌍

    def is_all_done(self) -> bool:
        """모든 시간 × 모든 방에 발송 완료되었는지"""
        total = len(self.times) * len(self.rooms)
        return len(self.fired) >= total

    def remaining_times(self) -> list:
        """아직 발송 안 된 시간 목록"""
        fired_times = {t for (t, _) in self.fired}
        return [t for t in self.times if t not in fired_times]

    def summary(self) -> str:
        times_str = ", ".join(self.times)
        rooms_str = " / ".join(self.rooms)
        img_icon  = " 🖼" if self.image_path else ""
        return f"⏰ {times_str}  |  💬 {rooms_str}{img_icon}"


# ─────────────────────────────────────────────
# 로그 패널 위젯
# ─────────────────────────────────────────────
class LogPanel(ctk.CTkFrame):
    """실시간 발송 로그를 표시하는 스크롤 가능한 텍스트 패널."""

    ICONS = {"info": "ℹ️", "success": "✅", "error": "❌", "warn": "⚠️"}

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkLabel(top, text="📜  발송 로그",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        ctk.CTkButton(top, text="지우기", width=70, height=26,
                      fg_color="#333", hover_color="#555",
                      command=self._clear).pack(side="right")

        self._box = ctk.CTkTextbox(
            self,
            state="disabled",
            font=ctk.CTkFont(family="Courier New", size=11),
            wrap="word",
        )
        self._box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def append(self, text: str, tag: str = "info"):
        now  = datetime.now().strftime("%H:%M:%S")
        icon = self.ICONS.get(tag, "•")
        line = f"[{now}] {icon}  {text}\n"
        self._box.configure(state="normal")
        self._box.insert("end", line)
        self._box.see("end")
        self._box.configure(state="disabled")

    def _clear(self):
        self._box.configure(state="normal")
        self._box.delete("0.0", "end")
        self._box.configure(state="disabled")


# ─────────────────────────────────────────────
# 시간 스핀박스 위젯
# ─────────────────────────────────────────────
class TimeSpinBox(ctk.CTkFrame):
    """
    ▲/▼ 버튼과 마우스 휠로 숫자를 조절하는 스핀박스.
    시간(0~23) 또는 분(0~59) 선택에 사용.

    [사용법]
    spin = TimeSpinBox(parent, min_val=0, max_val=23, initial=9, width=76)
    spin.pack(side="left")
    value = spin.get()  # "09" 형식 문자열 반환
    """

    def __init__(self, parent, min_val: int, max_val: int,
                 initial: int = 0, width: int = 76, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self._min = min_val
        self._max = max_val
        self._val = max(min_val, min(max_val, initial))
        self._var = ctk.StringVar(value=f"{self._val:02d}")

        _btn_kw = dict(
            width=width, height=22, corner_radius=6,
            fg_color="#1e3a5f", hover_color="#2a5a9f",
            font=ctk.CTkFont(size=11, weight="bold"),
        )

        # ▲ 버튼
        self._up = ctk.CTkButton(self, text="▲", command=self._inc, **_btn_kw)
        self._up.pack(fill="x", pady=(0, 2))

        # 값 표시 입력창
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, width=width, height=38,
            justify="center", font=ctk.CTkFont(size=17, weight="bold"),
        )
        self._entry.pack(fill="x")

        # ▼ 버튼
        self._dn = ctk.CTkButton(self, text="▼", command=self._dec, **_btn_kw)
        self._dn.pack(fill="x", pady=(2, 0))

        # 마우스 휠 바인딩 (Windows/macOS/Linux 공통)
        for widget in (self, self._entry, self._up, self._dn):
            widget.bind("<MouseWheel>", self._on_scroll)   # Windows / macOS
            widget.bind("<Button-4>",   lambda _e: self._inc())  # Linux scroll↑
            widget.bind("<Button-5>",   lambda _e: self._dec())  # Linux scroll↓

    # ── 값 증가 / 감소 ─────────────────────────────────────
    def _inc(self):
        self._set(self._val + 1 if self._val < self._max else self._min)

    def _dec(self):
        self._set(self._val - 1 if self._val > self._min else self._max)

    def _set(self, val: int):
        self._val = val
        self._var.set(f"{val:02d}")

    def _on_scroll(self, event):
        """마우스 휠 이벤트: 위로 굴리면 증가, 아래로 굴리면 감소."""
        if event.delta > 0:
            self._inc()
        else:
            self._dec()

    # ── 값 읽기 ────────────────────────────────────────────
    def get(self) -> str:
        """현재 값을 "HH" 형식 문자열로 반환 (직접 입력도 검증)."""
        try:
            v = int(self._var.get())
            v = max(self._min, min(self._max, v))
            self._val = v
            return f"{v:02d}"
        except (ValueError, TypeError):
            return f"{self._val:02d}"


# ─────────────────────────────────────────────
# 메인 애플리케이션
# ─────────────────────────────────────────────
class KakaoSchedulerApp(ctk.CTk):
    """
    카카오톡 예약 메세지 자동 발송 프로그램의 메인 윈도우.

    [탭 구성]
    - ⚙️ 예약 설정: 발송 시간, 채팅방, 메세지, 이미지 입력
    - 📋 예약 목록: 등록된 예약 현황 및 삭제
    - 📜 발송 로그: 실시간 발송 이력

    [스케줄러]
    - 백그라운드 데몬 스레드가 1초마다 현재 시각과 예약 시간을 비교
    - 일치하면 kakao_sender.send_to_room() 호출 (별도 스레드로 UI 블로킹 방지)
    """

    def __init__(self):
        super().__init__()

        self.title("📱  카카오톡 예약 메세지 자동 발송")
        self.geometry("800x820")
        self.minsize(700, 660)

        # 인메모리 예약 저장소
        self.schedules: dict = {}  # { uuid: ScheduleEntry }

        # 설정 탭 임시 상태
        self._time_chips:  list = []
        self._image_path:  str  = ""

        self._running = True

        self._build_ui()
        self._start_scheduler_thread()
        self._tick_clock()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ══════════════════════════════════════════
    #  UI 빌드
    # ══════════════════════════════════════════
    def _build_ui(self):
        # ── 타이틀 바 ──
        title_bar = ctk.CTkFrame(self, fg_color=KAKAO_BROWN, corner_radius=0)
        title_bar.pack(fill="x")

        ctk.CTkLabel(
            title_bar,
            text="💬  카카오톡 예약 메세지 자동 발송",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=KAKAO_YELLOW,
            pady=16,
        ).pack()

        # ── macOS 경고 배너 (Windows에서는 숨김) ──
        if not IS_WINDOWS:
            warn = ctk.CTkFrame(self, fg_color="#3d1a00", corner_radius=0)
            warn.pack(fill="x")
            ctk.CTkLabel(
                warn,
                text="⚠️  지금은 macOS 환경입니다. GUI 설정·미리보기는 가능하지만 실제 카카오톡 발송은 Windows에서만 작동합니다.",
                text_color="#ffaa44",
                font=ctk.CTkFont(size=11),
                pady=7,
                wraplength=740,
            ).pack()

        # ── 탭뷰 ──
        self._tabs = ctk.CTkTabview(self, anchor="nw")
        self._tabs.pack(fill="both", expand=True, padx=14, pady=(10, 0))

        self._tabs.add("⚙️  예약 설정")
        self._tabs.add("📋  예약 목록")
        self._tabs.add("📜  발송 로그")
        self._tabs.add("🎯  좌표 가이드")

        self._build_tab_settings(self._tabs.tab("⚙️  예약 설정"))
        self._build_tab_list(self._tabs.tab("📋  예약 목록"))
        self._build_tab_log(self._tabs.tab("📜  발송 로그"))
        self._build_tab_coord(self._tabs.tab("🎯  좌표 가이드"))

        # ── 상태 바 ──
        sbar = ctk.CTkFrame(self, fg_color="#0a0a14", corner_radius=0, height=30)
        sbar.pack(fill="x", side="bottom")
        sbar.pack_propagate(False)

        self._status_var = ctk.StringVar(value="대기 중...")
        ctk.CTkLabel(sbar, textvariable=self._status_var,
                     font=ctk.CTkFont(size=11), text_color="#aaa").pack(side="left", padx=10)

        self._clock_var = ctk.StringVar(value="")
        ctk.CTkLabel(sbar, textvariable=self._clock_var,
                     font=ctk.CTkFont(size=11), text_color=KAKAO_YELLOW).pack(side="right", padx=10)

    # ── 설정 탭 ──────────────────────────────────────────────
    def _build_tab_settings(self, tab):
        outer = ctk.CTkScrollableFrame(tab)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        # ── 발송 시간 ──
        t_card = self._card(outer, "⏰  발송 시간  (여러 개 추가 가능)")
        ctk.CTkLabel(t_card,
                     text="▲▼ 버튼 또는 마우스 휠로 시간 조정 →  '＋ 시간 추가' 클릭으로 등록",
                     text_color="#888", font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=14)

        t_row = ctk.CTkFrame(t_card, fg_color="transparent")
        t_row.pack(fill="x", padx=14, pady=8)

        # 시 스핀박스 (0~23)
        self._hour_spin = TimeSpinBox(t_row, min_val=0, max_val=23, initial=9, width=76)
        self._hour_spin.pack(side="left")
        ctk.CTkLabel(t_row, text="  시  ",
                     font=ctk.CTkFont(size=14)).pack(side="left")

        # 분 스핀박스 (0~59)
        self._min_spin = TimeSpinBox(t_row, min_val=0, max_val=59, initial=0, width=76)
        self._min_spin.pack(side="left")
        ctk.CTkLabel(t_row, text="  분  ",
                     font=ctk.CTkFont(size=14)).pack(side="left")

        ctk.CTkButton(t_row, text="＋ 시간 추가", width=106,
                      command=self._add_time_chip).pack(side="left", padx=(14, 0))

        # 추가된 시간 칩 목록
        self._chip_frame = ctk.CTkScrollableFrame(t_card, height=52, corner_radius=8,
                                                   orientation="horizontal")
        self._chip_frame.pack(fill="x", padx=14, pady=(4, 14))
        self._redraw_chips()

        # ── 발송 대상 채팅방 ──
        r_card = self._card(outer, "💬  발송 대상 채팅방")
        ctk.CTkLabel(r_card,
                     text="여러 방은 쉼표(,)로 구분. 카카오톡 채팅방 이름과 정확히 일치해야 합니다.",
                     text_color="#888", font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=14)

        self._room_entry = ctk.CTkEntry(r_card,
                                         placeholder_text="예: 우리 팀방, 마케팅 공지, 고객 문의방",
                                         height=38, font=ctk.CTkFont(size=13))
        self._room_entry.pack(fill="x", padx=14, pady=(6, 14))

        # ── 텍스트 메세지 ──
        m_card = self._card(outer, "✏️  전송할 메세지")
        ctk.CTkLabel(m_card,
                     text="Enter로 줄바꿈 가능. 카카오톡 Shift+Enter 변환은 자동 처리됩니다.",
                     text_color="#888", font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=14)

        self._msg_box = ctk.CTkTextbox(m_card, height=120, font=ctk.CTkFont(size=13),
                                        wrap="word", corner_radius=8)
        self._msg_box.pack(fill="x", padx=14, pady=(4, 14))

        # ── 이미지 첨부 ──
        i_card = self._card(outer, "🖼  첨부 이미지  (선택사항)")
        ctk.CTkLabel(i_card,
                     text="PNG, JPG, GIF, BMP, WEBP 지원. 클립보드 붙여넣기 방식으로 전송됩니다.",
                     text_color="#888", font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=14)

        i_row = ctk.CTkFrame(i_card, fg_color="transparent")
        i_row.pack(fill="x", padx=14, pady=(4, 14))

        ctk.CTkButton(i_row, text="📂 이미지 선택", width=115,
                      command=self._select_image).pack(side="left")

        self._img_label = ctk.CTkLabel(i_row, text="선택된 이미지 없음",
                                        text_color="#555", anchor="w", wraplength=420)
        self._img_label.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ctk.CTkButton(i_row, text="✕", width=30, height=30,
                      fg_color="#444", hover_color="#800",
                      command=self._clear_image).pack(side="right")

        # ── 예약 추가 버튼 ──
        ctk.CTkButton(
            outer,
            text="✅  예약 추가하기",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=50,
            fg_color="#1a5e32",
            hover_color="#154d28",
            command=self._add_schedule,
        ).pack(fill="x", padx=2, pady=(8, 16))

    # ── 예약 목록 탭 ─────────────────────────────────────────
    def _build_tab_list(self, tab):
        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.pack(fill="x", padx=4, pady=(8, 4))

        ctk.CTkLabel(hdr, text="📋  등록된 예약",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        ctk.CTkButton(hdr, text="🗑 완료 삭제", width=100, height=30,
                      fg_color="#444", hover_color="#800",
                      command=self._remove_done).pack(side="right")

        self._list_frame = ctk.CTkScrollableFrame(tab, corner_radius=10)
        self._list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self._draw_list()

    # ── 로그 탭 ──────────────────────────────────────────────
    def _build_tab_log(self, tab):
        self._log = LogPanel(tab, corner_radius=0)
        self._log.pack(fill="both", expand=True)

    # ── 좌표 가이드 탭 ───────────────────────────────────────
    def _build_tab_coord(self, tab):
        outer = ctk.CTkScrollableFrame(tab)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        # ── 경고 카드 ─────────────────────────────────────────
        warn = ctk.CTkFrame(outer, corner_radius=12, fg_color="#3d1a00")
        warn.pack(fill="x", padx=2, pady=(6, 4))
        ctk.CTkLabel(warn, text="📌  카카오톡 실행 전 반드시 확인하세요!",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="#ffaa44", anchor="w").pack(fill="x", padx=16, pady=(12, 5))
        for line in [
            "① 카카오톡 PC버전을 실행해 주세요",
            "② 아래 그림처럼  채팅방 목록이 보이는 상태여야 합니다",
            "③ 카카오톡 창을 최소화하거나 다른 창 뒤에 숨기지 마세요",
        ]:
            ctk.CTkLabel(warn, text=line, text_color="#ffcc88",
                         font=ctk.CTkFont(size=12), anchor="w").pack(
                         fill="x", padx=24, pady=1)
        ctk.CTkFrame(warn, fg_color="transparent", height=10).pack()

        # ── 레이아웃 다이어그램 카드 ──────────────────────────
        diag_card = self._card(outer, "📐  카카오톡 창 구조 & 클릭 순서")
        ctk.CTkLabel(diag_card,
                     text="프로그램이 ①②③ 순서로 해당 위치를 자동 클릭합니다",
                     text_color="#888", font=ctk.CTkFont(size=11),
                     anchor="w").pack(fill="x", padx=16, pady=(0, 6))

        # Canvas 컨테이너
        cv_wrap = ctk.CTkFrame(diag_card, fg_color="#0d0d1a", corner_radius=8)
        cv_wrap.pack(fill="x", padx=16, pady=(0, 14))

        canvas = tk.Canvas(cv_wrap, width=500, height=300,
                           bg="#0d0d1a", highlightthickness=0)
        canvas.pack(padx=12, pady=12)
        self._draw_kakao_diagram(canvas)

        # ── 좌표 미세 조정 카드 ───────────────────────────────
        adj_card = self._card(outer, "🔧  클릭 좌표 미세 조정")
        ctk.CTkLabel(adj_card,
                     text="PC 환경마다 카카오톡 창 위치가 다를 수 있어요.\n"
                          "실제로 안 맞는 항목만 숫자를 바꾸고 '적용' 버튼을 눌러주세요.",
                     text_color="#888", font=ctk.CTkFont(size=11),
                     justify="left", anchor="w").pack(fill="x", padx=16, pady=(0, 8))

        # 좌표 조정 항목 정의
        self._coord_vars: dict = {}
        rows = [
            ("chat_tab_x_offset",    "① 채팅 탭  X  (창 왼쪽에서 →)",   "px",   40, 200),
            ("chat_tab_y_ratio",     "① 채팅 탭  Y  (창 높이 비율 %)",   "%",     5,  60),
            ("search_x_from_right",  "② 검색버튼 X  (창 오른쪽에서 ←)", "px",   60, 250),
            ("search_y_from_top",    "② 검색버튼 Y  (창 위쪽에서 ↓)",   "px",   25, 120),
            ("input_tab_offset",     "③ 입력창 X  (사이드바+목록 폭)",   "px",  150, 500),
            ("input_y_from_bottom",  "③ 입력창 Y  (창 아래쪽에서 ↑)",   "px",   20, 120),
        ]
        defaults = {
            "chat_tab_x_offset":   75,
            "chat_tab_y_ratio":    30,   # % 표시 (내부에서 /100)
            "search_x_from_right": 130,
            "search_y_from_top":    55,
            "input_tab_offset":    330,
            "input_y_from_bottom":  50,
        }

        for key, label, unit, mn, mx in rows:
            row = ctk.CTkFrame(adj_card, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=3)

            ctk.CTkLabel(row, text=label, width=260,
                         anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")

            var = ctk.StringVar(value=str(defaults[key]))
            self._coord_vars[key] = var

            entry = ctk.CTkEntry(row, textvariable=var, width=70,
                                 justify="center", font=ctk.CTkFont(size=13))
            entry.pack(side="left", padx=(0, 6))

            ctk.CTkLabel(row, text=unit, text_color="#888",
                         font=ctk.CTkFont(size=11)).pack(side="left")

        # 적용 버튼
        ctk.CTkButton(
            adj_card,
            text="✅  좌표 적용",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=44, fg_color="#1a5e32", hover_color="#154d28",
            command=self._apply_coords,
        ).pack(fill="x", padx=16, pady=(10, 16))

    # ── 좌표 적용 핸들러 ─────────────────────────────────────
    def _apply_coords(self):
        """입력된 좌표 값을 kakao_sender에 적용한다."""
        try:
            for key, var in self._coord_vars.items():
                raw = var.get().strip()
                if key == "chat_tab_y_ratio":
                    update_coord(key, float(raw) / 100.0)
                else:
                    update_coord(key, int(float(raw)))
            self._set_status("🎯 좌표 설정이 적용되었습니다")
            self._log.append("좌표 설정 적용 완료", "info")
            from tkinter import messagebox
            messagebox.showinfo("완료", "좌표가 적용되었습니다!\n이제 발송을 테스트해보세요.")
        except ValueError as e:
            from tkinter import messagebox
            messagebox.showerror("오류", f"숫자만 입력해주세요.\n{e}")

    # ── 카카오톡 레이아웃 다이어그램 그리기 ──────────────────
    @staticmethod
    def _draw_kakao_diagram(canvas: tk.Canvas):
        """
        Canvas에 카카오톡 창 구조와 클릭 위치를 그린다.

        [레이아웃]
        ┌────────────────────────────────────── 전체 창 ──┐
        │ [제목표시줄]                          [🔍] ②   │
        ├──────┬───────────────────────────────────────── │
        │      │  채팅방 목록                   채팅 내용 │
        │  아  │  ──────────────────────                 │
        │  이  │  채팅방 이름                            │
        │  ①  │  ──────────────────────                 │
        │  콘  │  채팅방 이름          ┌────────────────┐│
        │      │                      │  메세지 입력 ③ ││
        └──────┴──────────────────────┴────────────────┘┘
        """
        W, H = 500, 300

        # ── 창 외곽 ──────────────────────────────────────────
        canvas.create_rectangle(8, 8, W - 8, H - 8,
                                 fill="#1e1e32", outline="#4477bb", width=2, tags="all")

        # ── 제목 표시줄 ──────────────────────────────────────
        TITLE_H = 30
        canvas.create_rectangle(8, 8, W - 8, 8 + TITLE_H,
                                 fill="#2a2a48", outline="#4477bb", width=1)
        canvas.create_text(W // 2, 8 + TITLE_H // 2,
                           text="카카오톡", fill="#cccccc",
                           font=("Arial", 10, "bold"))
        # 창 버튼 (❌ 모양)
        for bx, bc in [(W - 26, "#cc4444"), (W - 50, "#888"), (W - 74, "#888")]:
            canvas.create_oval(bx - 7, 17, bx + 7, 31,
                               fill=bc, outline="")

        # ── 영역 경계 ─────────────────────────────────────────
        CONTENT_TOP = 8 + TITLE_H
        SIDEBAR_R   = 68          # 사이드바 오른쪽 x
        LIST_R      = 230         # 채팅목록 오른쪽 x
        ROOM_R      = W - 8       # 채팅룸 오른쪽 x
        BOTTOM      = H - 8

        # 사이드바
        canvas.create_rectangle(8, CONTENT_TOP, SIDEBAR_R, BOTTOM,
                                 fill="#141428", outline="#333355", width=1)
        canvas.create_text((8 + SIDEBAR_R) // 2, (CONTENT_TOP + BOTTOM) // 2,
                           text="아\n이\n콘", fill="#555577",
                           font=("Arial", 9))

        # 채팅방 목록
        canvas.create_rectangle(SIDEBAR_R, CONTENT_TOP, LIST_R, BOTTOM,
                                 fill="#191930", outline="#333355", width=1)
        # 목록 헤더
        canvas.create_rectangle(SIDEBAR_R, CONTENT_TOP, LIST_R, CONTENT_TOP + 28,
                                 fill="#22223a", outline="#333355")
        canvas.create_text((SIDEBAR_R + LIST_R) // 2, CONTENT_TOP + 14,
                           text="채팅", fill="#aaaacc", font=("Arial", 9, "bold"))

        # 채팅방 목록 항목 (3개)
        for i, name in enumerate(["팀 공지방", "프로젝트 A팀", "마케팅팀"]):
            y0 = CONTENT_TOP + 28 + i * 44
            canvas.create_rectangle(SIDEBAR_R + 2, y0 + 2,
                                     LIST_R - 2, y0 + 42,
                                     fill="#1e1e38", outline="#2a2a50", width=1)
            canvas.create_oval(SIDEBAR_R + 10, y0 + 10,
                                SIDEBAR_R + 32, y0 + 32,
                                fill="#2a4a7a", outline="")
            canvas.create_text(SIDEBAR_R + 75, y0 + 21,
                               text=name, fill="#aaaacc", font=("Arial", 9))

        # 채팅룸 영역
        canvas.create_rectangle(LIST_R, CONTENT_TOP, ROOM_R, BOTTOM,
                                 fill="#111120", outline="#333355", width=1)

        # 메세지 버블 (예시)
        for i, (txt, side) in enumerate([("안녕하세요!", "left"),
                                          ("네, 안녕하세요 😊", "right")]):
            y_b = CONTENT_TOP + 20 + i * 50
            if side == "right":
                bx1, bx2 = ROOM_R - 110, ROOM_R - 14
                fill = "#FEE500"
                fc = "#333"
            else:
                bx1, bx2 = LIST_R + 14, LIST_R + 120
                fill = "#2a3a5a"
                fc = "#ccc"
            canvas.create_rectangle(bx1, y_b, bx2, y_b + 28,
                                     fill=fill, outline="", width=0)
            canvas.create_text((bx1 + bx2) // 2, y_b + 14,
                               text=txt, fill=fc, font=("Arial", 8))

        # 입력창 영역 ③
        INPUT_Y = BOTTOM - 45
        canvas.create_rectangle(LIST_R + 4, INPUT_Y, ROOM_R - 4, BOTTOM - 4,
                                 fill="#22223a", outline="#4466aa", width=2)
        canvas.create_text((LIST_R + ROOM_R) // 2, INPUT_Y + 20,
                           text="메세지를 입력하세요...",
                           fill="#555577", font=("Arial", 9))

        # ── 구분선 ───────────────────────────────────────────
        canvas.create_line(LIST_R, CONTENT_TOP, LIST_R, BOTTOM,
                           fill="#4477bb", width=1)
        canvas.create_line(SIDEBAR_R, CONTENT_TOP, SIDEBAR_R, BOTTOM,
                           fill="#4477bb", width=1)

        # ── 클릭 마커 ①②③ 그리기 헬퍼 ─────────────────────
        def marker(x, y, num, color, label):
            r = 14
            canvas.create_oval(x - r, y - r, x + r, y + r,
                                fill=color, outline="white", width=2)
            canvas.create_text(x, y, text=num,
                                fill="black", font=("Arial", 10, "bold"))
            # 설명 라벨 (마커 옆)
            lx = x + r + 5
            ly = y
            canvas.create_text(lx + 1, ly + 1, text=label,
                                fill="#000000", anchor="w", font=("Arial", 8))
            canvas.create_text(lx, ly, text=label,
                                fill=color, anchor="w", font=("Arial", 8))

        # ① 채팅 탭: 사이드바 중앙, 창 높이 30%
        m1x = (8 + SIDEBAR_R) // 2
        m1y = CONTENT_TOP + int((BOTTOM - CONTENT_TOP) * 0.30)
        marker(m1x, m1y, "①", "#FEE500", "채팅 탭\nleft+75px\n높이×30%")

        # ② 검색 버튼: right-130px, top+55px
        m2x = W - 8 - 20          # 다이어그램 기준 오른쪽 130px 지점
        m2y = 8 + 20
        # 배치상 채팅목록 헤더 오른쪽 끝으로
        m2x = LIST_R - 18
        m2y = CONTENT_TOP + 14
        marker(m2x, m2y, "②", "#44aaff", "검색 버튼\nright-130px\ntop+55px")

        # ③ 입력창: 채팅룸 중앙, bottom-50px
        m3x = (LIST_R + ROOM_R) // 2
        m3y = INPUT_Y + 20
        marker(m3x, m3y, "③", "#44dd88", "입력창\n채팅영역 중앙\nbottom-50px")

        # ── 범례 ─────────────────────────────────────────────
        # (다이어그램 내부 하단에 작게)
        legend_y = BOTTOM - 5
        for i, (num, col, desc) in enumerate([
            ("①", "#FEE500", "채팅탭 클릭"),
            ("②", "#44aaff", "검색 클릭"),
            ("③", "#44dd88", "입력창 클릭"),
        ]):
            lx = 12 + i * 100
            canvas.create_oval(lx, legend_y - 10, lx + 14, legend_y + 4,
                                fill=col, outline="")
            canvas.create_text(lx + 7, legend_y - 3, text=num,
                                fill="black", font=("Arial", 7, "bold"))
            canvas.create_text(lx + 50, legend_y - 3, text=desc,
                                fill="#888", font=("Arial", 8))

    # ── 헬퍼: 카드 프레임 ────────────────────────────────────
    @staticmethod
    def _card(parent, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, corner_radius=12)
        frame.pack(fill="x", padx=2, pady=5)
        ctk.CTkLabel(frame, text=title,
                     font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        return frame

    # ══════════════════════════════════════════
    #  설정 탭 이벤트 핸들러
    # ══════════════════════════════════════════
    def _add_time_chip(self):
        t = f"{self._hour_spin.get()}:{self._min_spin.get()}"
        if t in self._time_chips:
            messagebox.showinfo("알림", f"'{t}' 은(는) 이미 추가되어 있습니다.")
            return
        self._time_chips.append(t)
        self._redraw_chips()

    def _remove_time_chip(self, t: str):
        if t in self._time_chips:
            self._time_chips.remove(t)
        self._redraw_chips()

    def _redraw_chips(self):
        for w in self._chip_frame.winfo_children():
            w.destroy()

        if not self._time_chips:
            ctk.CTkLabel(self._chip_frame, text="추가된 시간 없음",
                         text_color="#555", font=ctk.CTkFont(size=12)).pack(side="left", padx=6)
            return

        for t in sorted(self._time_chips):
            chip = ctk.CTkFrame(self._chip_frame, corner_radius=20, fg_color="#1e3a5f")
            chip.pack(side="left", padx=3, pady=2)
            ctk.CTkLabel(chip, text=t, font=ctk.CTkFont(size=12), padx=10).pack(side="left")
            ctk.CTkButton(chip, text="✕", width=22, height=22, corner_radius=11,
                          fg_color="#333", hover_color="#800",
                          command=lambda x=t: self._remove_time_chip(x)).pack(side="left", padx=(0, 4))

    def _select_image(self):
        path = filedialog.askopenfilename(
            title="이미지 파일 선택",
            filetypes=[("이미지 파일", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                       ("모든 파일", "*.*")]
        )
        if path:
            self._image_path = path
            self._img_label.configure(
                text=f"📎  {os.path.basename(path)}", text_color="#ccc"
            )

    def _clear_image(self):
        self._image_path = ""
        self._img_label.configure(text="선택된 이미지 없음", text_color="#555")

    def _add_schedule(self):
        # ── 입력값 검증 ──
        if not self._time_chips:
            messagebox.showwarning("입력 오류", "발송 시간을 하나 이상 추가해주세요.")
            return

        rooms_raw = self._room_entry.get().strip()
        if not rooms_raw:
            messagebox.showwarning("입력 오류", "발송 대상 채팅방 이름을 입력해주세요.")
            return

        rooms   = [r.strip() for r in rooms_raw.split(",") if r.strip()]
        message = self._msg_box.get("0.0", "end").strip()

        if not message and not self._image_path:
            messagebox.showwarning("입력 오류", "전송할 메세지 또는 이미지를 하나 이상 설정해주세요.")
            return

        # ── 예약 생성 ──
        entry = ScheduleEntry(
            times      = list(self._time_chips),
            rooms      = rooms,
            message    = message,
            image_path = self._image_path,
        )
        self.schedules[entry.id] = entry

        # ── 입력 초기화 ──
        self._time_chips.clear()
        self._redraw_chips()
        self._room_entry.delete(0, "end")
        self._msg_box.delete("0.0", "end")
        self._clear_image()

        # ── UI 갱신 ──
        self._draw_list()
        self._tabs.set("📋  예약 목록")
        self._set_status(f"✅ 예약이 추가되었습니다: {entry.summary()}")
        self._log.append(f"예약 추가: {entry.summary()}", "info")

    # ══════════════════════════════════════════
    #  예약 목록 UI
    # ══════════════════════════════════════════
    def _draw_list(self):
        for w in self._list_frame.winfo_children():
            w.destroy()

        if not self.schedules:
            ctk.CTkLabel(self._list_frame,
                         text="아직 예약이 없습니다. '예약 설정' 탭에서 추가해보세요.",
                         text_color="#555", font=ctk.CTkFont(size=13)).pack(pady=40)
            return

        # 첫 번째 남은 발송 시간 기준 정렬
        sorted_entries = sorted(
            self.schedules.values(),
            key=lambda e: (e.remaining_times() or e.times)[0]
        )
        for entry in sorted_entries:
            self._draw_card(entry)

    def _draw_card(self, entry: ScheduleEntry):
        done  = entry.is_all_done()
        card  = ctk.CTkFrame(
            self._list_frame, corner_radius=10,
            fg_color="#1b3050" if not done else "#1a1a1a",
            border_width=1,
            border_color="#3a6ea8" if not done else "#333",
        )
        card.pack(fill="x", padx=4, pady=5)

        # 상단 행
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 4))

        remaining = entry.remaining_times()
        if done:
            status_txt = "✅  모든 발송 완료"
            status_col = "#555"
        else:
            status_txt = f"🔔  대기 중  |  남은 발송: {', '.join(remaining)}"
            status_col = "#5ea3e0"

        ctk.CTkLabel(top, text=status_txt,
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=status_col).pack(side="left")

        ctk.CTkButton(top, text="삭제", width=52, height=26,
                      fg_color="#333", hover_color="#800",
                      command=lambda eid=entry.id: self._delete_entry(eid)).pack(side="right")

        # 세부 정보
        def info_line(icon, text, color="#aaa"):
            ctk.CTkLabel(card, text=f"{icon}  {text}", text_color=color,
                         font=ctk.CTkFont(size=12), anchor="w").pack(fill="x", padx=16, pady=1)

        info_line("⏰", "  /  ".join(entry.times))
        info_line("💬", "  /  ".join(entry.rooms))

        preview = entry.message[:60].replace("\n", " ↵ ") + ("…" if len(entry.message) > 60 else "")
        if preview:
            info_line("📝", preview, "#888")

        if entry.image_path:
            info_line("🖼", os.path.basename(entry.image_path), "#888")

        # 하단 여백
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _delete_entry(self, eid: str):
        if eid in self.schedules:
            del self.schedules[eid]
        self._draw_list()

    def _remove_done(self):
        done_ids = [eid for eid, e in self.schedules.items() if e.is_all_done()]
        for eid in done_ids:
            del self.schedules[eid]
        self._draw_list()
        self._set_status(f"🗑  완료 예약 {len(done_ids)}개 삭제")

    # ══════════════════════════════════════════
    #  상태 바 / 시계
    # ══════════════════════════════════════════
    def _set_status(self, msg: str):
        self._status_var.set(msg)

    def _tick_clock(self):
        self._clock_var.set(f"🕐  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
        self.after(1000, self._tick_clock)

    # ══════════════════════════════════════════
    #  백그라운드 스케줄러
    # ══════════════════════════════════════════
    def _start_scheduler_thread(self):
        self._running = True
        t = threading.Thread(target=self._scheduler_loop, daemon=True)
        t.start()

    def _scheduler_loop(self):
        """
        1초마다 현재 시각을 확인하여 예약 시간과 대조한다.

        [중복 발송 방지 로직]
        - fired_this_minute: set에 (entry_id, time, room) 키를 기록
        - 매 분이 시작될 때(hhmm이 바뀌면) 이 set을 초기화
        - 0~4초 구간에서만 체크하여 정각 타이밍을 놓쳐도 5초 이내 재시도 가능
        """
        fired_this_minute: set = set()
        last_hhmm = ""

        while self._running:
            now      = datetime.now()
            hhmm     = now.strftime("%H:%M")
            sec      = now.second

            # 매 분 초기화
            if hhmm != last_hhmm:
                fired_this_minute.clear()
                last_hhmm = hhmm

            # 정각 기준 0~4초 이내에만 발송
            if sec <= 4:
                for entry in list(self.schedules.values()):
                    if hhmm in entry.times:
                        for room in entry.rooms:
                            key = (entry.id, hhmm, room)
                            if key not in fired_this_minute and key not in entry.fired:
                                fired_this_minute.add(key)
                                # UI 스레드 안전 실행
                                self.after(
                                    0,
                                    lambda e=entry, t=hhmm, r=room: self._fire(e, t, r)
                                )

            time.sleep(1)

    def _fire(self, entry: ScheduleEntry, fire_time: str, room: str):
        """
        실제 발송을 별도 스레드에서 실행 (UI 블로킹 방지).
        발송 완료 후 entry.fired 에 기록하고 목록 UI 갱신.
        """
        def _run():
            self._log.append(f"[{fire_time}] '{room}' 발송 시작...", "info")
            self._set_status(f"📤  발송 중... {fire_time} → {room}")

            result = send_to_room(
                room_name  = room,
                message    = entry.message,
                image_path = entry.image_path if entry.image_path else None,
            )

            if result["success"]:
                self._log.append(f"[{fire_time}] '{room}' 발송 성공", "success")
            else:
                self._log.append(
                    f"[{fire_time}] '{room}' 발송 실패: {result['error']}", "error"
                )

            # 발송 완료 기록 (성공/실패 모두 fired 처리하여 무한 재시도 방지)
            entry.fired.add((fire_time, room))

            # UI 갱신 (메인 스레드로)
            self.after(0, self._draw_list)
            self.after(0, lambda: self._set_status(
                f"{'✅' if result['success'] else '❌'}  {fire_time} → {room} 완료"
            ))

        threading.Thread(target=_run, daemon=True).start()

    # ══════════════════════════════════════════
    #  종료
    # ══════════════════════════════════════════
    def _on_close(self):
        self._running = False
        self.destroy()


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # PyInstaller .exe 실행 시 작업 디렉토리 보정
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    app = KakaoSchedulerApp()
    app.mainloop()
