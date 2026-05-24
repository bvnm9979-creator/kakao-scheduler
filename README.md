# 📅 예약 메세지 팝업 프로그램

지정한 시간이 되면 텍스트 + 이미지가 담긴 팝업 창이 자동으로 화면에 나타나는 Windows 데스크톱 프로그램입니다.

---

## 주요 기능

- ⏰ **예약 시간 설정** — 시/분 드롭다운으로 직관적 설정
- ✏️ **텍스트 메세지** — 자유롭게 내용 입력
- 🖼 **이미지 첨부** — PNG, JPG, GIF 등 모든 이미지 형식 지원
- 🔔 **자동 팝업** — 예약 시간이 되면 화면 최상단에 팝업 표시
- 📋 **다중 예약** — 여러 개의 예약을 동시에 등록 가능
- 🗑 **완료 항목 정리** — 발송된 예약 일괄 삭제

## 기술 스택

| 항목 | 내용 |
|------|------|
| 언어 | Python 3.11 |
| GUI | CustomTkinter 5.x |
| 이미지 처리 | Pillow (PIL) |
| 빌드 | PyInstaller |
| CI/CD | GitHub Actions (Windows 환경) |

## 개발 환경에서 실행하기 (macOS)

```bash
# 1. 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 실행
python main.py
```

## Windows .exe 빌드하기

GitHub에 Push하면 자동으로 빌드됩니다.
→ **전체 가이드는 아래 [스텝 가이드] 참고**
