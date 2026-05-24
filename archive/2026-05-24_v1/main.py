"""
=============================================================
  예약 메세지 팝업 윈도우 — 메인 프로그램
  개발환경: macOS / 운영환경: Windows (.exe)
  GUI: CustomTkinter  |  스케줄러: threading + time
=============================================================
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import time
from datetime import datetime
from PIL import Image, ImageTk
import os
import sys
import uuid  # 각 예약에 고유 ID 부여용


# ─────────────────────────────────────────────
# 전역 설정: CustomTkinter 테마
# ─────────────────────────────────────────────
ctk.set_appearance_mode("dark")        # "dark" / "light" / "system"
ctk.set_default_color_theme("blue")   # "blue" / "green" / "dark-blue"


# ─────────────────────────────────────────────
# 팝업 윈도우 클래스 (예약 시간에 열리는 창)
# ─────────────────────────────────────────────
class PopupWindow(ctk.CTkToplevel):
    """
    지정된 시간이 되면 화면에 나타나는 메세지 팝업 창.
    텍스트 메세지와 이미지를 함께 표시한다.
    """

    def __init__(self, parent, message: str, image_path: str = None, schedule_time: str = ""):
        super().__init__(parent)

        self.title(f"📢 예약 메세지 — {schedule_time}")
        self.geometry("520x580")
        self.resizable(True, True)

        # 항상 위에 표시 (다른 창 위로 올라옴)
        self.attributes("-topmost", True)
        self.lift()
        self.focus_force()

        # ── 상단: 제목 바 ──
        header = ctk.CTkFrame(self, fg_color="#1f538d", corner_radius=0)
        header.pack(fill="x")

        ctk.CTkLabel(
            header,
            text=f"🔔  예약 메세지  |  {schedule_time}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="white",
            pady=14
        ).pack()

        # ── 스크롤 가능한 내용 영역 ──
        scroll_frame = ctk.CTkScrollableFrame(self, corner_radius=10)
        scroll_frame.pack(fill="both", expand=True, padx=16, pady=12)

        # 이미지가 있으면 표시
        if image_path and os.path.isfile(image_path):
            try:
                pil_img = Image.open(image_path)
                # 최대 480×320 으로 비율 유지 축소
                pil_img.thumbnail((480, 320), Image.LANCZOS)
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                       size=(pil_img.width, pil_img.height))
                img_label = ctk.CTkLabel(scroll_frame, image=ctk_img, text="")
                img_label.pack(pady=(8, 12))
                # 가비지컬렉션 방지용 참조 보관
                img_label._ctk_image_ref = ctk_img
            except Exception as e:
                ctk.CTkLabel(
                    scroll_frame,
                    text=f"⚠️ 이미지를 불러올 수 없습니다: {e}",
                    text_color="#ff6b6b"
                ).pack(pady=8)

        # 텍스트 메세지 표시
        ctk.CTkLabel(
            scroll_frame,
            text=message if message else "(메세지 없음)",
            font=ctk.CTkFont(size=15),
            wraplength=460,
            justify="left",
            anchor="w"
        ).pack(fill="x", padx=12, pady=(4, 16))

        # ── 하단: 닫기 버튼 ──
        ctk.CTkButton(
            self,
            text="✅  확인 (닫기)",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=44,
            command=self.destroy
        ).pack(fill="x", padx=16, pady=(0, 16))


# ─────────────────────────────────────────────
# 메인 애플리케이션 클래스
# ─────────────────────────────────────────────
class SchedulerApp(ctk.CTk):
    """
    예약 메세지 팝업 프로그램의 메인 윈도우.
    - 발송 시간, 메세지, 이미지를 설정하고 예약을 추가한다.
    - 백그라운드 스레드가 1초마다 현재 시각과 예약 시간을 비교한다.
    - 일치하면 팝업 창을 연다.
    """

    def __init__(self):
        super().__init__()

        self.title("📅 예약 메세지 팝업 프로그램")
        self.geometry("700x750")
        self.minsize(600, 600)

        # ── 인메모리 예약 목록 ──
        # 구조: { uuid: {"time": "HH:MM", "message": str, "image_path": str, "fired": bool} }
        self.schedules: dict = {}

        # 선택된 이미지 경로 (설정 화면용 임시 변수)
        self.selected_image_path = ctk.StringVar(value="")

        # UI 구성
        self._build_ui()

        # 백그라운드 스케줄러 스레드 시작
        self._start_scheduler_thread()

        # 창 닫을 때 스레드 안전 종료
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 빌드 ──────────────────────────────────────────────
    def _build_ui(self):
        """전체 UI 레이아웃을 구성한다."""

        # ── 상단 타이틀 ──
        title_frame = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=0)
        title_frame.pack(fill="x")

        ctk.CTkLabel(
            title_frame,
            text="🗓  예약 메세지 팝업 프로그램",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#5ea3e0",
            pady=18
        ).pack()

        # ── 설정 입력 패널 ──
        input_card = ctk.CTkFrame(self, corner_radius=14)
        input_card.pack(fill="x", padx=20, pady=(16, 8))

        ctk.CTkLabel(
            input_card,
            text="⚙️  새 예약 설정",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w"
        ).pack(fill="x", padx=16, pady=(14, 6))

        # ── 발송 시간 입력 ──
        time_row = ctk.CTkFrame(input_card, fg_color="transparent")
        time_row.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(time_row, text="⏰  발송 시간", width=110, anchor="w").pack(side="left")

        self.hour_var = ctk.StringVar(value="09")
        self.min_var  = ctk.StringVar(value="00")

        hour_spin = ctk.CTkOptionMenu(
            time_row,
            variable=self.hour_var,
            values=[f"{h:02d}" for h in range(24)],
            width=80
        )
        hour_spin.pack(side="left", padx=(4, 2))

        ctk.CTkLabel(time_row, text="시", width=20).pack(side="left")

        min_spin = ctk.CTkOptionMenu(
            time_row,
            variable=self.min_var,
            values=[f"{m:02d}" for m in range(60)],
            width=80
        )
        min_spin.pack(side="left", padx=(8, 2))

        ctk.CTkLabel(time_row, text="분", width=20).pack(side="left")

        # ── 텍스트 메세지 입력 ──
        ctk.CTkLabel(
            input_card, text="✏️  메세지 내용", anchor="w"
        ).pack(fill="x", padx=16, pady=(10, 2))

        self.msg_textbox = ctk.CTkTextbox(
            input_card,
            height=110,
            font=ctk.CTkFont(size=13),
            wrap="word",
            corner_radius=8
        )
        self.msg_textbox.pack(fill="x", padx=16, pady=(0, 8))
        self.msg_textbox.insert("0.0", "여기에 보낼 메세지를 입력하세요.")

        # ── 이미지 첨부 ──
        img_row = ctk.CTkFrame(input_card, fg_color="transparent")
        img_row.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(img_row, text="🖼  첨부 이미지", width=110, anchor="w").pack(side="left")

        ctk.CTkButton(
            img_row,
            text="파일 선택",
            width=90,
            command=self._select_image
        ).pack(side="left", padx=(4, 8))

        self.img_label = ctk.CTkLabel(
            img_row,
            textvariable=self.selected_image_path,
            text_color="#888",
            anchor="w",
            wraplength=350
        )
        self.img_label.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            img_row,
            text="✕",
            width=30,
            fg_color="#555",
            hover_color="#800",
            command=self._clear_image
        ).pack(side="right")

        # ── 예약 추가 버튼 ──
        ctk.CTkButton(
            input_card,
            text="＋  예약 추가",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=42,
            command=self._add_schedule
        ).pack(fill="x", padx=16, pady=(4, 16))

        # ── 예약 목록 패널 ──
        list_header = ctk.CTkFrame(self, fg_color="transparent")
        list_header.pack(fill="x", padx=20, pady=(4, 0))

        ctk.CTkLabel(
            list_header,
            text="📋  예약 목록",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w"
        ).pack(side="left")

        ctk.CTkButton(
            list_header,
            text="🗑  완료 항목 삭제",
            width=140,
            height=30,
            fg_color="#555",
            hover_color="#800",
            command=self._remove_fired
        ).pack(side="right")

        # 스크롤 가능한 예약 목록
        self.list_frame = ctk.CTkScrollableFrame(self, corner_radius=12)
        self.list_frame.pack(fill="both", expand=True, padx=20, pady=(6, 8))

        self._empty_label = ctk.CTkLabel(
            self.list_frame,
            text="아직 예약이 없습니다. 위에서 추가해보세요!",
            text_color="#666",
            font=ctk.CTkFont(size=13)
        )
        self._empty_label.pack(pady=30)

        # ── 하단 상태 바 ──
        self.status_var = ctk.StringVar(value="대기 중...")
        status_bar = ctk.CTkFrame(self, fg_color="#111", corner_radius=0, height=32)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)

        ctk.CTkLabel(
            status_bar,
            textvariable=self.status_var,
            font=ctk.CTkFont(size=12),
            text_color="#aaa"
        ).pack(side="left", padx=12, pady=6)

        # 현재 시각 표시 레이블
        self.clock_var = ctk.StringVar()
        ctk.CTkLabel(
            status_bar,
            textvariable=self.clock_var,
            font=ctk.CTkFont(size=12),
            text_color="#5ea3e0"
        ).pack(side="right", padx=12, pady=6)

        # 시계 업데이트 시작
        self._update_clock()

    # ── 기능 메서드 ──────────────────────────────────────────
    def _update_clock(self):
        """현재 시각을 상태 바에 1초마다 업데이트한다."""
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.clock_var.set(f"🕐 현재 시각: {now}")
        self.after(1000, self._update_clock)  # 1초 후 재호출

    def _select_image(self):
        """파일 탐색기를 열어 이미지를 선택한다."""
        path = filedialog.askopenfilename(
            title="이미지 파일 선택",
            filetypes=[
                ("이미지 파일", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("모든 파일", "*.*")
            ]
        )
        if path:
            # 파일명만 표시 (경로가 길 경우 보기 좋게)
            filename = os.path.basename(path)
            self.selected_image_path.set(path)
            self.img_label.configure(text=f"📎 {filename}", textvariable=None)

    def _clear_image(self):
        """선택된 이미지를 제거한다."""
        self.selected_image_path.set("")
        self.img_label.configure(text="선택된 이미지 없음", textvariable=None,
                                  text_color="#666")

    def _add_schedule(self):
        """입력값을 검증하고 예약 목록에 추가한다."""
        hour = self.hour_var.get()
        minute = self.min_var.get()
        schedule_time = f"{hour}:{minute}"

        # 메세지 내용 가져오기
        message = self.msg_textbox.get("0.0", "end").strip()

        if not message or message == "여기에 보낼 메세지를 입력하세요.":
            messagebox.showwarning("입력 오류", "메세지 내용을 입력해주세요.")
            return

        image_path = self.selected_image_path.get()

        # 고유 ID로 예약 저장 (인메모리)
        schedule_id = str(uuid.uuid4())
        self.schedules[schedule_id] = {
            "time":       schedule_time,
            "message":    message,
            "image_path": image_path,
            "fired":      False   # 아직 발송 안 됨
        }

        # 입력 초기화
        self.msg_textbox.delete("0.0", "end")
        self.msg_textbox.insert("0.0", "여기에 보낼 메세지를 입력하세요.")
        self._clear_image()

        # 목록 UI 갱신
        self._refresh_list()
        self.status_var.set(f"✅ {schedule_time} 예약이 추가되었습니다.")

    def _refresh_list(self):
        """예약 목록 UI를 현재 self.schedules 데이터에 맞게 다시 그린다."""
        # 기존 위젯 모두 삭제
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        if not self.schedules:
            self._empty_label = ctk.CTkLabel(
                self.list_frame,
                text="아직 예약이 없습니다. 위에서 추가해보세요!",
                text_color="#666",
                font=ctk.CTkFont(size=13)
            )
            self._empty_label.pack(pady=30)
            return

        # 시간 순 정렬
        sorted_items = sorted(self.schedules.items(), key=lambda x: x[1]["time"])

        for sch_id, sch in sorted_items:
            self._add_schedule_card(sch_id, sch)

    def _add_schedule_card(self, sch_id: str, sch: dict):
        """예약 목록에 카드 형태의 UI 항목을 하나 추가한다."""
        is_fired = sch["fired"]

        card = ctk.CTkFrame(
            self.list_frame,
            corner_radius=10,
            fg_color="#1e3a5f" if not is_fired else "#2a2a2a",
            border_width=1,
            border_color="#3a6ea8" if not is_fired else "#444"
        )
        card.pack(fill="x", padx=4, pady=4)

        # ── 카드 상단 행: 시간 + 삭제 버튼 ──
        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(10, 2))

        status_icon = "🔔" if not is_fired else "✅"
        ctk.CTkLabel(
            top_row,
            text=f"{status_icon}  {sch['time']}",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#5ea3e0" if not is_fired else "#888"
        ).pack(side="left")

        ctk.CTkButton(
            top_row,
            text="삭제",
            width=56,
            height=26,
            fg_color="#444",
            hover_color="#800",
            command=lambda sid=sch_id: self._delete_schedule(sid)
        ).pack(side="right")

        # ── 메세지 미리보기 ──
        preview = sch["message"]
        if len(preview) > 60:
            preview = preview[:60] + "..."

        ctk.CTkLabel(
            card,
            text=preview,
            text_color="#ccc" if not is_fired else "#666",
            font=ctk.CTkFont(size=12),
            anchor="w",
            wraplength=480
        ).pack(fill="x", padx=16, pady=(0, 4))

        # ── 이미지 여부 표시 ──
        img_text = f"🖼  {os.path.basename(sch['image_path'])}" if sch["image_path"] else "이미지 없음"
        ctk.CTkLabel(
            card,
            text=img_text,
            text_color="#888",
            font=ctk.CTkFont(size=11),
            anchor="w"
        ).pack(fill="x", padx=16, pady=(0, 10))

    def _delete_schedule(self, sch_id: str):
        """특정 예약을 목록에서 삭제한다."""
        if sch_id in self.schedules:
            del self.schedules[sch_id]
        self._refresh_list()
        self.status_var.set("🗑  예약이 삭제되었습니다.")

    def _remove_fired(self):
        """발송 완료된 예약만 일괄 삭제한다."""
        to_delete = [sid for sid, s in self.schedules.items() if s["fired"]]
        for sid in to_delete:
            del self.schedules[sid]
        self._refresh_list()
        self.status_var.set(f"🗑  완료 항목 {len(to_delete)}개가 삭제되었습니다.")

    # ── 스케줄러 백그라운드 스레드 ─────────────────────────
    def _start_scheduler_thread(self):
        """메인 스레드가 멈추지 않도록 백그라운드 스레드에서 시간을 확인한다."""
        self._running = True
        t = threading.Thread(target=self._scheduler_loop, daemon=True)
        t.start()

    def _scheduler_loop(self):
        """
        1초마다 현재 시:분 과 예약 목록의 시간을 비교한다.
        일치하는 예약이 있으면 메인 스레드(after)를 통해 팝업을 연다.
        """
        while self._running:
            now_hhmm = datetime.now().strftime("%H:%M")
            now_sec   = datetime.now().second

            # 정각(0초)일 때만 체크 → 중복 발송 방지
            if now_sec == 0:
                for sch_id, sch in list(self.schedules.items()):
                    if not sch["fired"] and sch["time"] == now_hhmm:
                        # 발송 완료 플래그 즉시 세팅 (중복 방지)
                        self.schedules[sch_id]["fired"] = True
                        # 팝업은 반드시 메인 스레드에서 열어야 함
                        self.after(0, lambda s=sch, t=now_hhmm: self._fire_popup(s, t))

            time.sleep(1)

    def _fire_popup(self, sch: dict, schedule_time: str):
        """팝업 창을 열고 예약 목록을 갱신한다."""
        popup = PopupWindow(
            parent=self,
            message=sch["message"],
            image_path=sch["image_path"],
            schedule_time=schedule_time
        )
        popup.grab_set()  # 팝업이 닫히기 전까지 메인 창 비활성화 (선택사항)
        self._refresh_list()
        self.status_var.set(f"🔔 {schedule_time} 예약 메세지가 발송되었습니다!")

    # ── 종료 처리 ────────────────────────────────────────────
    def _on_close(self):
        """프로그램 종료 시 스레드를 안전하게 멈추고 창을 닫는다."""
        self._running = False
        self.destroy()


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # PyInstaller로 exe를 만들었을 때 경로 문제 방지
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    app = SchedulerApp()
    app.mainloop()
