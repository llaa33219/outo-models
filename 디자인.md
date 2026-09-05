# BLP Minimal Tile

> **BLP 컬러 팔레트 기반의 미니멀 타일링 디자인 언어.**
> 모든 화면은 "타일"의 집합으로 사고하고, 타일 안의 "요소"로 채운다.
> *큰 UI부터 작은 UI, 도구 데모까지* — 단, **UI 크롬(타일/버튼/인풋)만 규칙 적용, 콘텐츠(이미지/영상/SVG)는 자유**.

---

## 0. 핵심 개념 한 줄 요약

### 0.1 적용 범위 — UI 크롬 vs 콘텐츠

> **이 디자인 언어의 모든 규칙은 "UI 크롬"에만 적용된다. 콘텐츠는 자유다.**

| 구분 | 정의 | 규칙 적용 |
| --- | --- | --- |
| **UI 크롬** | 타일, 버튼, 인풋, 리스트, 네비, 폼 컨트롤, 보더, 배경 색 등 *인터페이스 자체* | **모든 규칙 적용** (BLP 팔레트, 완전 각, 그라데이션 X 등) |
| **콘텐츠** | 이미지, 사진, 영상, SVG 일러스트, 3D 캔버스, 코드 에디터 뷰포트 등 *사용자가 보거나 다루는 대상* | **자유.** 그라데이션·다채로운 색·블러 그림자·알록달록 다 허용. |

```
┌─ 배경 타일 (UI 크롬 — BLP 규칙 적용)
│  ┌─ 요소 타일 (UI 크롬)
│  │  ┌─────────────────────────────┐
│  │  │  콘텐츠 영역                 │
│  │  │  (이미지/영상/SVG/3D 캔버스) │ ← 자유
│  │  │  - 풀 컬러 사진 가능         │
│  │  │  - 그라데이션 영상 가능       │
│  │  │  - 3D 렌더 / 차트 / 코드 뷰  │
│  │  │  - SVG 일러스트 가능          │
│  │  └─────────────────────────────┘
│  └─ (타일 자체는 미니멀, 콘텐츠만 풍부)
│
└─ (보더, 패딩, gap은 BLP 규칙 그대로)
```

> **원칙:** *무대(타일)는 미니멀하게, 무대 위의 작품(콘텐츠)은 화려하게.*

### 0.2 사용처 (Scope)

이 언어는 *어떤 종류의 화면에도* 적용 가능하다. 타일 구조 + 콘텐츠 자유의 조합.

| 카테고리 | 예시 | 특징 |
| --- | --- | --- |
| **고밀도 정보 UI** | 어드민, 대시보드, CRM, 에디터, 메일 | 작은 타일, 4px gap, 1000px 캡 — *기본 패턴* |
| **채팅 / 협업** | 메시지 UI, 화상회의, 문서 공동편집 | 좌우 분할, 입력 패널, 사이드 패널 |
| **e-commerce** | 쇼핑몰, 상품 비교, 결제 플로우 | 네비/필터/그리드 3분할 |
| **마케팅 / 광고** | 모델 소개, 제품 런칭, 캠페인 페이지 | **큰 히어로 이미지, 풀블리드 비디오 OK**. 타일 크기 제약 X. |
| **리포트 / 브로슈어** | 모델 카드, 제품 스펙, 백서 | 큰 카드 + 작은 통계 타일 혼합 |
| **도구 시연** | 3D 뷰어, 물리엔진 데모, 코드 에디터, 그래프 빌더 | 큰 캔버스(콘텐츠) + 미니멀 컨트롤 패널(크롬) |
| **문서 / 블로그** | 기술 문서, 매뉴얼, 포스트 | 본문 폭 캡 + 사이드바(TOC) |

> **제약 조건은 *UI 크롬*에만 적용**되므로, 위의 모든 케이스에서 미니멀한 인터페이스를 유지하면서 풍부한 콘텐츠를 담을 수 있다.

### 0.3 핵심 용어

| 구분 | 의미 |
| --- | --- |
| **타일 (Tile)** | 컨테이너 자체. 배경 또는 의미 단위의 박스. |
| **요소 (Element)** | 타일 *안에 들어가는* 구성 단위 (버튼, 입력, 카드, 리스트 아이템 등). |
| **배경 타일 (Background Tile)** | 화면을 분할하는 비-오버랩 타일. WM 타일링처럼 격자 배치. |
| **요소 타일 (Element Tile)** | 어디든 배치 가능한 자유 크기 타일. 배경 타일 *안에* 놓거나 *플로팅(fixed)* 으로 띄움. |
| **UI 크롬 (UI Chrome)** | 인터페이스의 *골격* — 타일, 버튼, 인풋, 보더, 배경. 규칙 적용 대상. |
| **콘텐츠 (Content)** | 사용자가 *보거나 다루는* 대상 — 이미지, 영상, SVG, 3D, 차트. 규칙 적용 안 됨. |

> **`[!]`** "타일"과 "요소"는 항상 분리해서 생각한다.
> 타일은 *무대*, 요소는 *무대 위의 물건*. 콘텐츠는 *무대 위의 작품*.

---

## 1. 타일 시스템

> **타일의 크기/종횡비/배치는 자유.** 작은 격자부터 화면을 가득 채우는 히어로 타일까지 허용.
> 다만 1.3의 타일 공통 규칙(보더, 모서리, 호버 패턴)은 *모든 크기*에 일관되게 적용.

### 1.1 배경 타일 (Background Tile)

- **용도**: 화면을 의미 단위로 분할. (예: 채팅의 메시지 영역 / 사이드바, 쇼핑몰의 네비게이션 / 상품 패널, *마케팅 페이지의 히어로 / 콘텐츠 섹션*)
- **배치**: 서로 겹치지 않음. Windows 타일링 WM 또는 i3 / Hyprland 같은 격자 레이아웃과 동일한 느낌.
- **Gap**: 타일과 타일 사이 **`4px`**.
- **내부 폭**: 배경 타일 *안의* 콘텐츠는 **기본적으로 최대 폭 1000px 미만**으로 제한 (가독성 / 집중).
  - **예외 — 큰 UI**: 마케팅 히어로, 제품 쇼케이스, 풀블리드 영상은 1000px 캡 없이 *화면 폭 전체* 사용 가능.
- **컨테이너 폭 자체는 자유**: 배경 타일 자체는 화면을 가득 채워도 됨. 단 *안에 들어가는 콘텐츠*만 1000px 캡 (위 예외 제외).

```html
<div class="bg-tile-grid">           <!-- 화면 전체 -->
  <section class="bg-tile">          <!-- 배경 타일 1: 사이드바 -->
    ...                              <!-- 폭 제한 없이 채움 -->
  </section>
  <section class="bg-tile">          <!-- 배경 타일 2: 메인 -->
    <div class="bg-tile__inner">     <!-- 1000px 캡 -->
      ...콘텐츠...
    </div>
  </section>
</div>
```

```css
.bg-tile-grid {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 4px;                          /* 배경 타일 간 gap */
  width: 100%;
  height: 100vh;
}
.bg-tile { background: var(--tile-bg); }
.bg-tile__inner {
  max-width: 1000px;                 /* 캡 < 1000px */
  margin: 0 auto;
  padding: 16px;
}
```

### 1.2 요소 타일 (Element Tile)

- **용도**: 정보 단위, 액션 그룹, 플로팅 패널 등.
- **배치**: 세 가지
  1. **배경 타일 안** — 정상 흐름 (in-flow). 자유 크기.
  2. **플로팅** — `position: fixed`. **스크롤과 함께 움직이지 않음**. (별도 레이어)
  3. **요소 타일 안** — 요소 타일이 또 다른 타일 역할을 할 수 있음 (재귀).
- **크기**: 자유. 콘텐츠에 맞게, 또는 명시적 사이즈.
- **내부 폭**: 자유. (배경 타일의 1000px 캡이 걸리지 않음)

```html
<!-- 1) 배경 타일 안 -->
<section class="bg-tile">
  <div class="el-tile">...</div>
</section>

<!-- 2) 플로팅 (스크롤 무시) -->
<div class="el-tile el-tile--floating">...</div>

<!-- 3) 요소 타일 안 -->
<div class="el-tile">
  <div class="el-tile el-tile--nested">...</div>
</div>
```

```css
.el-tile--floating {
  position: fixed;
  top: 16px; right: 16px;
}
```

### 1.3 타일 공통 규칙

| 속성 | 값 |
| --- | --- |
| **태두리 두께** | `2px` (요소 타일도 동일하게 2px) |
| **태두리 색** | 비호버 = `inactive` / 호버 = `--color-main` |
| **배경** | `--tile-bg` (`#FAFCFF`) |
| **모서리** | **항상 완전 각 (`border-radius: 0`). 둥글 옵션 없음.** |

> **`[!]`** **타일은 둥글 수 없다.** 배경 타일이든 요소 타일이든 모두 `border-radius: 0`.
> 둥글 옵션은 오직 *타일 안의 작은 요소*(버튼, 인풋, 칩, 아바타 등)에만 허용된다 (→ 2.1 참고).
> 부분 둥근 형태 (`8px` 등)는 어디에도 허용되지 않는다.

---

## 2. 요소 (Element) 스타일링

타일 *안에* 들어가는 구성 단위. 버튼, 입력, 칩, 리스트 아이템 등.

> "요소 타일" (큰 컨테이너 박스, 카드류) 은 1.3의 타일 규칙을 따른다 — **항상 완전 각**.
> 이 장에서 말하는 "요소"는 *타일 내부의 작은 컴포넌트*만 해당한다.

### 2.1 모서리

작은 요소(버튼, 인풋, 칩, 아바타 등)에 한해서만 이분법 적용:

- **둥글** → `border-radius: <height> / 2`  (캡슐 / 라운드 사각)
- **각** → `border-radius: 0`  (사각)

> 같은 종류의 요소는 페이지 안에서 *일관*되게 한쪽만 쓴다. (버튼은 둥글, 인풋은 둥글, 식으로 통일)

### 2.2 태두리

| 속성 | 값 |
| --- | --- |
| **태두리 두께** | `1px` (요소는 타일보다 얇음) |
| **기본 색** | `inactive` |
| **호버 색** | `active` |
| **포커스 색** | `active` + 추가로 `--focus-ring` (`#0026A3`) 1px inner outline 또는 색 강조 |

### 2.3 상태 머신

```
default  → inactive border
:hover   → active border + (요소 한정) 호버 색상 변화
:active  → focus 색상 (`#0026A3`)
:focus   → focus 색상 (`#0026A3`)
disabled → inactive border 50% + 텍스트 50% (배경은 유지)
```

### 2.4 배경 타일 안 요소 vs 요소 타일 안 요소

| | 배경 타일 안 요소 | 요소 타일 안 요소 |
| --- | --- | --- |
| 폭 | 부모 배경 타일 내부 캡 (1000px)을 따른다 | 자유 |
| 정렬 | 배경 타일의 `__inner` 내부 정렬을 따름 | 자기 자신이 컨테이너 |

---

## 3. 컬러 토큰

BLP 팔레트에서 추출. *이름은 디자인 의도*, *값은 hex*.

### 3.1 코어 토큰 (필수)

| 토큰 | hex | 사용처 |
| --- | --- | --- |
| `--color-main` | `#007BFF` | 메인 액센트, **태두리 활성 색상** |
| `--color-inactive` | `#94989E` | **태두리 비활성 색상**, 비활성 UI |
| `--tile-bg` | `#FAFCFF` | 타일 배경 (BLP WHITE) |
| `--text` | `#000000` | 본문 텍스트 |
| `--text-sub` | `#3E4D5F` | 서브 텍스트 (BLP SUB DARK) |
| `--el-hover` | `#005BDD` | 요소 호버 색상 (BLP DEEP BLUE) |
| `--el-active` | `#0026A3` | 요소 액티브 / 포커스 색상 (BLP ULTRA DEEP BLUE) |

### 3.2 보조 팔레트 (필요시 사용)

> 코어 토큰만으로 부족할 때, 아래 BLP 정의 색상 중 *의미가 명확한 것*만 골라 사용한다.

| 토큰 | hex | 이름 | 권장 용도 |
| --- | --- | --- | --- |
| `--blp-blue` | `#007BFF` | BLP BLUE | 메인 (= `--color-main`) |
| `--blp-sky` | `#00AFFF` | BLP LOGO SKY BLUE | 보조 액센트 |
| `--blp-deep-blue` | `#006DBD` | BLP LOGO DARK BLUE | 강조 텍스트 |
| `--blp-light-blue` | `#DBEDFF` | BLP LIGHT BLUE | 약한 강조 배경 |
| `--blp-paper-blue` | `#EEF5FC` | BLP BG BLUE | 알림/뱃지 약 배경 |
| `--blp-paper-blue-2` | `#F4F8FC` | BLP PAPER BLUE | 미세한 톤 차이 |
| `--blp-white` | `#FAFCFF` | BLP WHITE | 타일 배경 |
| `--blp-soft-dark-blue` | `#7F9BFF` | BLP SOFT DARK BLUE | 약 액센트, 비활성 액션 |
| `--blp-soft-blue` | `#7FBCFF` | BLP SOFT BLUE | 비활성/플레이스홀더 |
| `--blp-light-dark` | `#D4DCE8` | BLP LIGHT DARK | 보더 약 |
| `--blp-light-dark-blue` | `#DBE3FF` | BLP LIGHT DARK BLUE | 배경 변형 |
| `--blp-deep-dark` | `#00193D` | BLP DARK | 진한 텍스트/헤더 |
| `--blp-deeper-dark` | `#000A19` | BLP DEEP DARK | 헤더 배경 |
| `--blp-ultra-dark` | `#000309` | BLP ULTRA DEEP DARK | 풀블리드 다크 |
| `--blp-green` | `#00D620` | BLP GREEN | 성공 |
| `--blp-deep-green` | `#00B21B` | BLP DEEP GREEN | 강조 성공 |
| `--blp-paper-green` | `#B8E1D8` | BLP BG GREEN | 성공 알림 약 배경 |
| `--blp-paper-lime` | `#F2F6ED` | BLP PAPER LIME | 성공 알림 미세 배경 |
| `--blp-red` | `#FF0505` | BLP RED | 에러/삭제 |
| `--blp-deep-red` | `#DB0000` | BLP DEEP RED | 강조 에러 |
| `--blp-soft-red` | `#FF7F7F` | BLP SOFT RED | 약한 경고 |
| `--blp-yellow` | `#FFEA00` | BLP YELLOW | 경고 |
| `--blp-deep-yellow` | `#EBD700` | BLP DEEP YELLOW | 강조 경고 |
| `--blp-orange` | `#D09C7B` | BLP BG BROWN | 보조 강조 |
| `--blp-paper-orange` | `#FCF5EE` | BLP BG ORANGE | 보조 약 배경 |
| `--blp-purple` | `#AE00FF` | BLP PURPLE | 보조 액센트 |
| `--blp-deep-purple` | `#9200D6` | BLP DEEP PURPLE | 보조 강조 |
| `--blp-soft-purple` | `#D67FFF` | BLP SOFT PURPLE | 보조 약 |

> 네온 색상(`NEON *`)과 BLP ULTRA DEEP 계열은 **타일 자체의 배경/보더에는 쓰지 않는다**.
> 데이터 시각화·임포트 아이콘 등 정보 표현용으로만 제한적으로.

### 3.3 다크 모드 (선택)

다크 모드가 필요하면:
- `--tile-bg` → `#00193D` (BLP DARK)
- `--text` → `#FAFCFF`
- `--text-sub` → `#D4DCE8` 또는 `#7F9BFF`
- `--color-inactive` → `#7F9BFF` 또는 톤 다운된 값

다크 모드는 **선택 사항**이며, 본 스펙의 필수 영역은 아니다.

### 3.4 색상 사용 규칙 (단일성)

BLP Minimal Tile에서 색은 *용도가 단일*하다.
**한 색은 한 가지 의미, 어디서든 같은 의미로만 쓴다.**
호버 색을 여기서는 호버로, 저기서는 "선택된 요소"로 재사용하는 일 없음.

#### 색상별 유일한 사용처

| 토큰 | hex | 유일한 사용처 |
| --- | --- | --- |
| `--color-main` | `#007BFF` | ① 메인 액센트 · ② **선택된(Selected) 요소의 색** · ③ **호버된 타일의 태두리** |
| `--el-hover` | `#005BDD` | ① **모든 요소의 호버 색상** (타일 호버 제외. 어디서든 단일) |
| `--el-active` | `#0026A3` | ① **모든 요소의 액티브 / 포커스 색상** (단일) |
| `--color-inactive` | `#94989E` | ① 비활성 / 기본 태두리 |
| `--tile-bg` | `#FAFCFF` | ① 타일 배경 |
| `--text` | `#000000` | ① 본문 텍스트 |
| `--text-sub` | `#3E4D5F` | ① 서브 텍스트 |

> 이 세 가지가 동일값이라는 것:
> **주 색상 = 선택된 요소 색상 = 호버된 타일 태두리 색상** (모두 `#007BFF`)
>
> 이 하나가 동일값이라는 것:
> **타일 호버를 제외한 모든 요소의 호버 색상** (모두 `#005BDD`)

#### 색 변화 규칙 (요소 *충실도*에 따라 분기)

> 호버 / 액티브 / 클릭 시점에서 *전경(텍스트) ↔ 배경*이 BLP BLUE ↔ BLP WHITE로 뒤집히는 패턴은 **절대 금지**다.
> 색은 *더 어둡게* 또는 *더 밝게*의 단방향으로만 흐른다. 라이트 ↔ 다크 swap 없음.

**호버는 요소가 "채워져 있는지"에 따라 분기:**

| 요소 종류 | 채움 여부 | 호버 시 변화 |
| --- | --- | --- |
| 타일 (배경/요소) | n/a | 태두리만 `--color-main` (#007BFF). 내부 변화 X. |
| 요소 (fill = 색이 칠해진) | **`[O]`** 채움 | 태두리가 *더 어두운 톤*으로. (`--el-hover` #005BDD) |
| 요소 (no fill = 흰/투명) | **`[X]`** 비어있음 | 태두리가 `--color-main` (#007BFF) 로. 내부 변화 X. |

> **판별 기준:** 요소의 배경이 `--tile-bg`와 시각적으로 구분되는 *별도의 색*이면 "채워짐". 같거나 거의 같으면 "비어있음".

```css
/* 채워진 요소 (예: primary 버튼 — 파란 배경) */
.primary:hover {
  border-color: var(--el-hover);   /* 더 어두운 파랑 */
  /* 또는: background를 더 어둡게 + 보더도 같이 */
}

/* 비어있는 요소 (예: default 버튼 — 흰 배경) */
.button:hover {
  border-color: var(--color-main); /* BLP BLUE */
  /* background 변화 X */
}
```

**액티브 / 포커스:**

| 요소 종류 | 채움 여부 | 액티브/포커스 시 변화 |
| --- | --- | --- |
| 타일 | n/a | (해당 없음. 영구 선택 상태만 가질 수 있음) |
| 채워진 요소 | **`[O]`** | 태두리 `--el-active` (#0026A3) 로. (배경 변화 선택적) |
| 비어있는 요소 | **`[X]`** | 태두리 `--el-active` + **내부 색상 변화 필수** (어두워지거나 밝아짐) |

> 비어있는 요소는 액티브/포커스 시 *반드시* 내부에 시각적 변화가 있어야 한다.
> 태두리만 바꾸고 끝내지 말 것. 배경이 paper-blue 톤이나 light-dark 톤으로 *한 단계* 이동.

```css
/* 채워진 요소 (메인 버튼) */
.primary:active, .primary:focus-visible {
  background: var(--el-active);
  border-color: var(--el-active);
}

/* 비어있는 요소 (default 버튼) — 내부 변화 필수 */
.button:active, .button:focus-visible {
  border-color: var(--el-active);
  background: var(--blp-light-dark-blue);   /* #DBE3FF — 내부 변화 */
}
```

**서브 요소(네비, 리스트)도 같은 분기 적용:**

```css
/* 비어있는 nav-item (기본 상태) */
.nav-item:hover { border-color: var(--color-main); }   /* BLP BLUE */

.nav-item:active, .nav-item:focus-visible {
  border-color: var(--el-active);
  background: var(--blp-light-dark-blue);             /* 내부 변화 필수 */
}
```

**예외: 영구 선택(`.selected` / `.active` 클래스):**
이건 *지속 상태*이지 호버/액티브 이벤트가 아니다. 메인 색(`--color-main`)으로 채우는 것이 허용된다 — 색반전이 아니라 *기본 상태*의 디자인 선택.

---

## 4. 타이포그래피

### 4.1 폰트

**Pretendard** 단일 패밀리. 모든 weight (100~900) 사용 가능.

```css
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Thin.woff2') format('woff2');
  font-weight: 100; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-ExtraLight.woff2') format('woff2');
  font-weight: 200; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Light.woff2') format('woff2');
  font-weight: 300; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Regular.woff2') format('woff2');
  font-weight: 400; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Medium.woff2') format('woff2');
  font-weight: 500; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-SemiBold.woff2') format('woff2');
  font-weight: 600; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Bold.woff2') format('woff2');
  font-weight: 700; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-ExtraBold.woff2') format('woff2');
  font-weight: 800; font-display: swap;
}
@font-face {
  font-family: 'Pretendard';
  src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/pretendard@1.0/Pretendard-Black.woff2') format('woff2');
  font-weight: 900; font-display: swap;
}
```

### 4.2 사이즈

> **자유.** 웹사이트의 성격에 맞춰 설계자가 결정.

권장 가이드라인 (참고용, 강제 아님):

| 역할 | weight | 권장 px |
| --- | --- | --- |
| Display | 800–900 | 48–72 |
| H1 | 700 | 32–40 |
| H2 | 700 | 24–32 |
| H3 | 600 | 20–24 |
| Body | 400 | 14–16 |
| Caption / Sub | 300–400 | 12–13 |
| Mono / Code | 400 | 13–14 |

행간은 1.4~1.6 권장. 자간은 본문 `0`, 헤딩 `-0.01em` 정도가 무난.

---

## 5. 인터랙션

### 5.1 타일 호버

| 상태 | 보더 색 | 비고 |
| --- | --- | --- |
| 기본 | `inactive` (`#94989E`) | 마우스 떠남 |
| **호버** | `--color-main` (`#007BFF`) | 마우스 진입 |
| (영구 선택 상태) | `--color-main` 채움 (배경/텍스트) | `.selected` 클래스 등 |

> "호버된 타일만 활성" → 화면 전체가 아니라 **마우스가 올라간 타일 하나만** 보더가 살아난다.
> 다른 모든 타일은 비활성. 이게 BLP Minimal Tile의 *시각적 리듬*.

### 5.2 요소 호버 — 채움 여부로 분기

**색반전 금지.** 디폴트 요소를 호버 시 *흰 배경 → 파란 배경 + 검은 텍스트 → 흰 텍스트*로 뒤집지 않는다.

| 요소 종류 | 채움 | 호버 시 변화 |
| --- | --- | --- |
| **메인 버튼** (파란 배경) | **`[O]`** | **더 어두운 파랑** (`--el-hover` #005BDD) — 배경·태두리 모두 |
| **디폴트 버튼 / 인풋 / 리스트 / 네비** (빈 배경) | **`[X]`** | **태두리만 `--color-main`** (#007BFF) |
| **칩** (paper 톤 배경) | **`[O]`** | 태두리가 더 어두운 톤 (`--el-hover` 또는 fill의 deep 버전) |

### 5.3 액티브 / 포커스 — 비어있는 요소는 내부 변화 필수

| 요소 종류 | 채움 | 액티브/포커스 시 변화 |
| --- | --- | --- |
| **메인 버튼** | **`[O]`** | 배경·태두리 `--el-active` (#0026A3) |
| **디폴트 버튼 / 인풋 / 리스트 / 네비** | **`[X]`** | 태두리 `--el-active` + **내부 색상 변화 필수** |
| **칩** | **`[O]`** | 보더 `--el-active` + 텍스트/배경 톤 다운 (선택) |

> **비어있는 요소의 액티브/포커스 = 태두리만 바꾸고 끝내지 말 것.**
> 반드시 *내부 색상* (보통 배경)이 한 단계 변화해야 한다.
> 권장: `--blp-light-dark-blue` (#DBE3FF) 또는 `--blp-light-dark` (#D4DCE8).

### 5.4 전환 타이밍

**타일 = 즉시 (0s). 요소 = 부드러운 전환.**

| 대상 | 토큰 | 값 |
| --- | --- | --- |
| **타일** (배경/요소) 호버 | `--t-tile` | `0s` *(애니메이션 없음)* |
| **요소** 기본 전환 | `--t-base` | `200ms ease-in-out` |
| **요소** 빠른 전환 | `--t-fast` | `100ms ease-in-out` |
| **요소** 느린 전환 | `--t-slow` | `300ms ease-in-out` |

> **타일은 무조건 즉시.** 호버/비호버의 *경계가 또렷*해야 "한 타일만 살아남" 느낌이 산다.
> 요소는 *미세하게 부드럽게* — 0.2s 기본. 빠르거나 느린 게 필요할 때만 명시.

```css
:root {
  --t-tile: 0s;
  --t-fast: 100ms ease-in-out;
  --t-base: 200ms ease-in-out;
  --t-slow: 300ms ease-in-out;
}

.bg-tile, .el-tile {
  transition: border-color var(--t-tile);   /* 즉시 */
}

.el--btn {
  transition: background var(--t-base),
              color var(--t-base),
              border-color var(--t-base);   /* 부드럽게 */
}
```

### 5.5 그림자 시스템 (선택적 — 적용 시 아래 패턴 따라야 함)

그림자는 *옵션*. 모든 요소에 적용하지 않는다. 적용한다면 **반드시 아래 시스템** 사용.

> **Y좌표 증가는 실제 CSS `transform: translateY(Npx)` 값 기준이다.**
> (시각적 위치가 아니라 transform 값)

| 상태 | box-shadow | transform | 의미 |
| --- | --- | --- | --- |
| **기본 (idle)** | `6px 6px 0 <color>` | `translateY(-6px)` | 떠 있음 — 그림자 6/6, 본체 6px 위 |
| **호버** | `2px 2px 0 <color>` | `translateY(-2px)` | 살짝 내려옴 — 2/2 그림자, 2px 위 |
| **엑티브 (클릭)** | `0 0 0 <color>` | `translateY(0)` | 안착 — 그림자 사라지고 본체 정위치 |

**규칙:** 그림자 변위 = Y축 translate 만큼. (떠있을수록 그림자가 본체에서 더 멀어진다.)
엑티브 시 그림자 0/0 = "안착" 메타포.

```css
.tile--lifted {
  background: var(--tile-bg);
  border: var(--border-tile) solid var(--color-inactive);
  box-shadow: 6px 6px 0 var(--color-inactive);
  transform: translateY(-6px);
  transition: transform var(--t-base),
              box-shadow var(--t-base),
              border-color var(--t-base);
}
.tile--lifted:hover {
  border-color: var(--color-main);
  box-shadow: 2px 2px 0 var(--color-main);
  transform: translateY(-2px);
}
.tile--lifted:active {
  border-color: var(--el-active);
  box-shadow: 0 0 0 var(--el-active);
  transform: translateY(0);
}
```

> 그림자 색은 기본 `inactive`, 호버 `main`, 엑티브 `active`로 *상태에 맞춰* 어두워진다.
> 또는 `--tile-bg`(같은 색)로 둘 수도 — 단 색 일관성 유지.
>
> **보더 두께와의 관계:** 보더가 2px로 얇아진 만큼, 그림자 변위(6/2/0)는 그대로 두는 게 *떠있음 메타포*가 더 또렷해진다. 더 얇은 그림자(예: 3/1/0)를 원하면 별도 시스템.

---

## 6. 금지 사항

> **`[!]` 모든 금지 규칙은 *UI 크롬*에만 적용된다.**
> *콘텐츠* (이미지/영상/SVG/3D/차트) 는 그라데이션이든 알록달록이든 블러 그림자든 **완전 자유**.

### 6.0 금지의 대상 — UI 크롬

이 장의 모든 금지 항목은 다음에 *만* 적용된다:
- 타일 (배경/요소)
- 버튼, 인풋, 칩, 아바타, 리스트 아이템 등 *인터페이스 구성요소*
- 보더, 배경색, 텍스트, 아이콘 (벡터 아이콘)
- 모든 트랜지션/애니메이션

콘텐츠(이미지, 사진, 영상, SVG 일러스트, 3D 캔버스, 차트, 코드 뷰포트 등)는 **어떤 비주얼도 허용**된다.

### 6.1 금지 목록

| **`[X]`** 금지 | 이유 |
| --- | --- |
| **UI 크롬에 그라데이션** (linear/radial/conic) | 미니멀 위반. *콘텐츠의 그라데이션은 OK* (예: 히어로 사진, 영상). |
| **UI 크롬에 흐릿한 그림자** (blur 있는 box-shadow) | 미니멀 위반. 각진(샤프) 그림자만 허용. *콘텐츠의 그림자는 OK* (예: 영상 속 그림자). |
| **부분 둥근 모서리** (예: `border-radius: 8px`) | 미니멀 위반. 완전 둥글 또는 완전 각. |
| **타일을 둥글게** (배경 타일이든 요소 타일이든) | 타일은 항상 완전 각 (1.3). 둥글 옵션은 작은 요소에만. |
| **중간 굵기 보더** (예: 3px) | 타일 2px, 요소 1px. 그 외 금지. |
| **타일 안 요소의 보더가 타일 보더보다 두꺼움** | 항상 얇아야 함 (2 > 1). |
| **호버/액티브/클릭 시 색반전** (BLP BLUE ↔ BLP WHITE swap) | 색은 *더 어둡거나 더 밝거나* 단방향만. swap 금지 (3.4). |
| **호버/액티브 색을 다른 곳 기본값으로 재사용** | 한 색 = 한 의미 (3.4). |
| **비어있는 요소의 액티브/포커스에서 내부 변화 누락** | 태두리만 바꾸고 끝내지 말 것. 배경이 paper/light-dark 톤으로 *한 단계* 이동 필수 (3.4). |
| **네온 색상 일반 UI 사용** | 정보 강조 외 금지. *콘텐츠는 OK*. |
| **이모지 사용** (그림 문자, 픽토그래픽 유니코드) | 미니멀 위반. 단어나 색상 토큰으로 대체. |

### 6.2 각진 그림자가 허용되는 경우 (참고: 5.5 그림자 시스템)

- `box-shadow: 2px 2px 0 var(--color-main);` 같은 *오프셋만 있고 블러 0*인 hard shadow.
- "미니멀한 픽셀 느낌"을 줄 때만.
- 다크 모드에서 깊이 표현 대안으로.
- **콘텐츠(이미지, 영상) 안의 그림자는 자유** (블러, 다층 그림자 다 OK).

```css
.tile--sharp-shadow {
  box-shadow: 2px 2px 0 var(--color-inactive);
}
.tile--sharp-shadow:hover {
  box-shadow: 2px 2px 0 var(--color-main);
}
```

---

## 7. 컴포넌트 레퍼런스

> 모든 컴포넌트 예시는 *채움/비어있음* 분기를 따른다 (3.4).
> 비어있는 요소의 액티브/포커스에는 **반드시 내부 변화**가 들어간다.

### 7.1 버튼

```html
<button class="el el--btn">확인</button>            <!-- 비어있음 -->
<button class="el el--btn el--btn--primary">제출</button>  <!-- 채워짐 -->
```

```css
/* === 비어있는 버튼 (default) === */
.el--btn {
  display: inline-flex; align-items: center; justify-content: center;
  height: 40px; padding: 0 20px;
  font: 500 14px/1 'Pretendard';
  color: var(--text);
  background: var(--tile-bg);          /* 비어있음 — tile-bg 와 동일 */
  border: 1px solid var(--color-inactive);
  border-radius: 20px;
  cursor: pointer;
  transition: background 120ms ease-out, border-color 120ms ease-out;
}

/* 비어있는 호버: 태두리만 --color-main. */
.el--btn:hover {
  border-color: var(--color-main);
}

/* 비어있는 액티브/포커스: 태두리 + 내부 변화 둘 다. */
.el--btn:active,
.el--btn:focus-visible {
  border-color: var(--el-active);
  background: var(--blp-light-dark-blue);  /* #DBE3FF — 내부 변화 (필수) */
  outline: none;
}

.el--btn--sharp { border-radius: 0; }

/* === 채워진 버튼 (primary) === */
.el--btn--primary {
  background: var(--color-main);    /* 채워짐 — #007BFF */
  color: var(--tile-bg);
  border-color: var(--color-main);
}

/* 채워진 호버: 더 어두운 톤으로. */
.el--btn--primary:hover {
  background: var(--el-hover);
  border-color: var(--el-hover);
  /* color: var(--tile-bg) 유지 — swap 아님 */
}

/* 채워진 액티브/포커스: 더 더 어둡게. */
.el--btn--primary:active,
.el--btn--primary:focus-visible {
  background: var(--el-active);
  border-color: var(--el-active);
}
```

### 7.2 입력 (비어있는 요소)

```html
<input class="el el--input" placeholder="입력…" />
```

```css
.el--input {
  height: 40px; padding: 0 14px;
  font: 400 14px/1 'Pretendard';
  color: var(--text);
  background: var(--tile-bg);          /* 비어있음 */
  border: 1px solid var(--color-inactive);
  border-radius: 20px;
  outline: none;
  transition: background 120ms ease-out, border-color 120ms ease-out;
}

/* 호버: 태두리만 --color-main. */
.el--input:hover { border-color: var(--color-main); }

/* 포커스: 태두리 --el-active + 내부 변화 (필수). */
.el--input:focus {
  border-color: var(--el-active);
  background: var(--blp-light-dark-blue);
}
.el--input::placeholder { color: var(--blp-soft-blue); }
```

### 7.3 카드 (요소 타일)

> 카드는 *요소 타일*이다. 1.3에 따라 **항상 완전 각**(`border-radius: 0`).
> 타일 호버 규칙 적용 — 태두리만 `--color-main`.

```html
<article class="el-tile el-tile--card">
  <h3>제목</h3>
  <p>설명 텍스트...</p>
</article>
```

```css
.el-tile--card {
  background: var(--tile-bg);
  border: var(--border-tile) solid var(--color-inactive);
  border-radius: 0;
  padding: 16px;
  transition: border-color 120ms ease-out;
}
.el-tile--card:hover {
  border-color: var(--color-main);
}
```

### 7.4 칩 / 태그 (채워진 요소)

> 칩은 paper 톤으로 채워진 작은 요소. 호버 시 더 어두운 톤.

```css
.el--chip {
  display: inline-flex; align-items: center;
  height: 24px; padding: 0 10px;
  font: 500 12px/1 'Pretendard';
  color: var(--text-sub);
  background: var(--blp-paper-blue);     /* 채워짐 */
  border: 1px solid var(--blp-light-blue);
  border-radius: 12px;                  /* 24/2 = 완전 둥글 */
}

/* 호버: 보더 더 어두운 톤. */
.el--chip:hover {
  border-color: var(--color-main);      /* 채워진 요소의 호버 = 더 어두운/메인 */
  color: var(--el-active);
}
```

### 7.5 리스트 아이템 (비어있는 요소)

```html
<div class="list-item">
  <div class="avatar">M</div>
  <div class="meta"><div class="name">이름</div><div class="desc">설명</div></div>
</div>
```

```css
.list-item {
  display: flex; align-items: center; gap: 12px;
  padding: 12px;
  background: var(--tile-bg);            /* 비어있음 */
  border: 1px solid var(--color-inactive);
  transition: background 120ms ease-out, border-color 120ms ease-out;
}

/* 비어있는 호버: 태두리만 --color-main. */
.list-item:hover {
  border-color: var(--color-main);
}

/* 비어있는 액티브: 태두리 + 내부 변화 (필수). */
.list-item:active {
  border-color: var(--el-active);
  background: var(--blp-light-dark-blue);
}
```

---

## 8. 레이아웃 패턴

> *기본 패턴*(8.1~8.3)은 작은 UI 기준. 큰 UI / 마케팅 / 도구 데모 패턴은 8.4~8.6 참고.

### 8.1 채팅 UI

```
┌─────────────┬───────────────────────┐
│             │                       │
│  사이드바    │   메시지 스트림       │  ← 배경 타일 1
│  (이력)      │                       │
│             │   ┌──────────────┐   │
│             │   │ 입력 타일     │   │  ← 배경 타일 2 (or 같은 타일 하단)
│             │   └──────────────┘   │
└─────────────┴───────────────────────┘
gap: 4px
```

### 8.2 대시보드

```
┌──────────┬──────────┬──────────┐
│  카드1   │  카드2   │  카드3   │
├──────────┴──────────┴──────────┤
│           메인 그래프           │
├──────────────────┬─────────────┤
│   리스트          │  사이드     │
└──────────────────┴─────────────┘
gap: 4px
```

### 8.3 쇼핑

```
┌────────────────────────────────┐
│           네비게이션 바         │
├────────────┬───────────────────┤
│  필터 패널 │   상품 그리드      │
│            │   ┌──┐ ┌──┐ ┌──┐  │
│            │   └──┘ └──┘ └──┘  │
│            │   ┌──┐ ┌──┐ ┌──┐  │
│            │   └──┘ └──┘ └──┘  │
└────────────┴───────────────────┘
gap: 4px
```

> 모든 패턴에서 *배경 타일끼리만 4px gap* 이다. 요소 타일끼리의 간격은 별도.

---

### 8.4 마케팅 / 제품 소개 (큰 UI)

> **타일 크기 제약 없음.** 히어로 / 콘텐츠 / 푸터 등 큰 섹션 타일 가능. 1000px 캡 미적용.
> 콘텐츠(이미지/영상)는 자유 — 그라데이션, 풀 컬러 사진, 큰 글자 다 OK.

```
┌─────────────────────────────────────────────────┐
│  네비 (배경 타일)                                │
├─────────────────────────────────────────────────┤
│                                                 │
│   히어로 타일 (배경 타일, 화면 폭 전체)         │
│   ┌─────────────────────────────────────┐      │
│   │   풀블리드 이미지 / 영상 / 3D 씬     │      │ ← 콘텐츠 자유
│   │   + 큰 헤드라인 (Pretendard 800-900)  │      │
│   │                                     │      │
│   └─────────────────────────────────────┘      │
│                                                 │
├──────────────────────┬──────────────────────────┤
│  섹션 2 (배경 타일)  │   섹션 3 (배경 타일)    │
│  텍스트 + 이미지      │   비디오 / SVG 그래픽   │
└──────────────────────┴──────────────────────────┘
gap: 4px
```

> 히어로 타일 안에 들어가는 콘텐츠(이미지/영상)는 그라데이션·블러·풀컬러 다 OK.
> 단, **타일 자체의 보더/배색은 BLP 규칙 그대로** 유지 — 미니멀 무대 + 화려한 작품.

### 8.5 모델 / 제품 리포트 (대형 카드 + 통계 타일)

```
┌─────────────────────────────────────────────────┐
│  헤더 (네비 + 제목)                              │
├─────────────────────────────────────────────────┤
│  히어로 사진 (풀블리드)                          │
├──────────┬──────────┬──────────┬────────────────┤
│ Spec 1   │ Spec 2   │ Spec 3   │ Spec 4         │  ← 작은 통계 타일
├──────────┴──────────┴──────────┼────────────────┤
│  본문 (1000px 캡)               │   사이드 TOC  │
└─────────────────────────────────┴────────────────┘
gap: 4px
```

> 본문 텍스트는 1000px 캡 (가독성), 히어오는 풀폭, 통계는 작은 타일.
> 이 *혼합*이 BLP Minimal Tile의 진짜 강점.

### 8.6 도구 시연 (3D / 물리엔진 / 에디터)

> **콘텐츠 영역(캔버스/뷰포트)은 100% 자유.** UI 크롬만 미니멀하게.

```
┌──────────┬──────────────────────────────────────┐
│  도구    │                                      │
│  패널    │     콘텐츠 영역 (캔버스 / 뷰포트)    │
│  (사이드) │   - 3D 렌더                         │
│          │   - 물리 시뮬레이션                    │
│  작은    │   - 차트 / 그래프                     │
│  컨트롤  │   - 코드 에디터                       │
│  타일들  │   - 미디어 플레이어                   │
│          │                                      │
│  ┌──┐    │   (어떤 비주얼이든 OK)               │
│  └──┘    │                                      │
│  ┌──┐    │                                      │
│  └──┘    │                                      │
│          │                                      │
├──────────┴──────────────────────────────────────┤
│  상태 바 / 타임라인                              │
└─────────────────────────────────────────────────┘
gap: 4px
```

> 캔버스는 *콘텐츠*이므로 그라데이션, 풀 컬러, 블러, 3D, 애니메이션 다 자유.
> 사이드 패널의 도구 버튼들은 *UI 크롬*이므로 BLP 규칙 적용.

---

## 9. 빠른 시작 (HTML 보일러플레이트)

```html
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>BLP Minimal Tile</title>
  <link rel="preconnect" href="https://cdn.jsdelivr.net" />
  <style>
    /* Pretendard @font-face (위 4.1 참고) */

    :root {
      --color-main:     #007BFF;   /* 메인 · 선택 색 · 타일 호버 보더 */
      --color-inactive: #94989E;   /* 비활성 / 기본 보더 */
      --tile-bg:        #FAFCFF;
      --text:           #000000;
      --text-sub:       #3E4D5F;
      --el-hover:       #005BDD;   /* 채워진 요소 호버 (어두운 톤) */
      --el-active:      #0026A3;   /* 모든 요소 액티브/포커스 (단일) */

      /* 트랜지션 */
      --t-tile: 0s;                        /* 타일 = 즉시 */
      --t-fast: 100ms ease-in-out;         /* 요소 빠른 */
      --t-base: 200ms ease-in-out;         /* 요소 기본 */
      --t-slow: 300ms ease-in-out;         /* 요소 느린 */
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100%; }
    body {
      font-family: 'Pretendard', system-ui, sans-serif;
      background: var(--tile-bg);
      color: var(--text);
    }

    /* 배경 타일 그리드 */
    .bg-tile-grid { display: grid; gap: 4px; height: 100vh; }

    /* 배경 타일 — 항상 완전 각. 호버는 즉시 전환. */
    .bg-tile {
      background: var(--tile-bg);
      border: var(--border-tile) solid var(--color-inactive);
      border-radius: 0;
      padding: 16px;
      overflow: auto;
      transition: border-color var(--t-tile);   /* 0s */
    }
    .bg-tile:hover { border-color: var(--color-main); }
    .bg-tile__inner { max-width: 1000px; margin: 0 auto; }

    /* 요소 타일 — 항상 완전 각. 호버는 즉시 전환. */
    .el-tile {
      background: var(--tile-bg);
      border: var(--border-tile) solid var(--color-inactive);
      border-radius: 0;
      transition: border-color var(--t-tile);
    }
    .el-tile:hover { border-color: var(--color-main); }
    .el-tile--floating { position: fixed; }

    /* 요소 — 둥글/각 선택 가능, 단 페이지 내 일관. 부드러운 전환. */
    .el {
      border: 1px solid var(--color-inactive);
      transition: background var(--t-base),
                  color var(--t-base),
                  border-color var(--t-base);
    }
    .el:hover { border-color: var(--color-main); }  /* 빈 요소 호버 기본 */
  </style>
</head>
<body>
  <div class="bg-tile-grid" style="grid-template-columns: 280px 1fr;">
    <aside class="bg-tile">사이드바</aside>
    <main class="bg-tile">
      <div class="bg-tile__inner">
        <div class="el-tile">콘텐츠</div>
      </div>
    </main>
  </div>
</body>
</html>
```

---

## 10. 체크리스트

새 화면을 만들 때 아래를 자가 점검:

- [ ] 화면은 *배경 타일* 들의 격자로 분할되어 있는가? (4px gap)
- [ ] 각 배경 타일의 콘텐츠는 1000px 캡 안인가?
- [ ] *요소 타일*은 의미 단위로 적절히 묶였는가?
- [ ] **모든 타일(배경/요소)의 모서리는 완전 각인가?**
- [ ] 타일 안 *작은 요소*(버튼/인풋/칩)는 *완전 둥글 또는 완전 각* 중 일관되게?
- [ ] 보더 두께는 타일 2px, 요소 1px인가?
- [ ] 타일 호버 시 보더가 `--color-main`인가?
- [ ] **요소가 채워져 있는가(별도 bg)?**
  - YES → 호버 보더는 더 어두운 톤 (`--el-hover` 또는 fill의 deep 버전)
  - NO → 호버 보더는 `--color-main`
- [ ] **비어있는 요소의 액티브/포커스에 *내부 색상 변화*가 들어갔는가?** (태두리만 바꾸고 끝 X)
- [ ] 호버/액티브/클릭 시 색반전이 일어나지 않는가?
- [ ] 선택된 요소의 색이 `--color-main`인가?
- [ ] 그라데이션 / 흐릿한 그림자 / 부분 둥근 모서리 어디에도 안 쓰는가?
- [ ] 색상은 모두 BLP 팔레트 안에서 골랐는가?
- [ ] **타일 호버는 0s (즉시), 요소 전환은 --t-base (200ms)인가?**
- [ ] 그림자 적용 시 떠있음→살짝내림→안착 시스템 따르는가? (5.5)
- [ ] 이모지를 안 쓰는가?
- [ ] 폰트는 Pretendard인가?

---

## 부록 A. CSS 변수 전체 덤프

```css
:root {
  /* 코어 */
  --color-main:     #007BFF;
  --color-inactive: #94989E;
  --tile-bg:        #FAFCFF;
  --text:           #000000;
  --text-sub:       #3E4D5F;
  --el-hover:       #005BDD;
  --el-active:      #0026A3;

  /* 보조 (BLP 팔레트) */
  --blp-sky:        #00AFFF;
  --blp-deep-blue:  #006DBD;
  --blp-light-blue: #DBEDFF;
  --blp-paper-blue: #EEF5FC;
  --blp-paper-blue-2:#F4F8FC;
  --blp-white:      #FAFCFF;
  --blp-soft-dark-blue: #7F9BFF;
  --blp-soft-blue:  #7FBCFF;
  --blp-light-dark: #D4DCE8;
  --blp-light-dark-blue: #DBE3FF;
  --blp-deep-dark:  #00193D;
  --blp-deeper-dark:#000A19;
  --blp-ultra-dark: #000309;

  --blp-green:      #00D620;
  --blp-deep-green: #00B21B;
  --blp-paper-green:#B8E1D8;
  --blp-paper-lime: #F2F6ED;

  --blp-red:        #FF0505;
  --blp-deep-red:   #DB0000;
  --blp-soft-red:   #FF7F7F;

  --blp-yellow:     #FFEA00;
  --blp-deep-yellow:#EBD700;

  --blp-orange:     #D09C7B;
  --blp-paper-orange:#FCF5EE;

  --blp-purple:     #AE00FF;
  --blp-deep-purple:#9200D6;
  --blp-soft-purple:#D67FFF;

  /* 시스템 상수 */
  --gap-tile: 4px;
  --border-tile: 2px;   /* 타일 보더 두께 (요소보다 두꺼움) */
  --border-el:   1px;   /* 요소 보더 두께 */

  /* 트랜지션 — 타일 즉시, 요소 부드럽게 */
  --t-tile: 0s;
  --t-fast: 100ms ease-in-out;
  --t-base: 200ms ease-in-out;
  --t-slow: 300ms ease-in-out;

  /* 그림자 (선택적 시스템 — 5.5 참고) */
  --lift-rest:  6px;  /* 기본 떠있을 때 그림자/translateY */
  --lift-hover: 2px;  /* 호버 시 */
  --lift-down:  0px;  /* 액티브(안착) */
}
```

---

## 부록 B. 의도와 적용 범위

- **"무대는 미니멀, 작품은 자유"** 가 핵심 원칙.
- UI 크롬(타일, 버튼, 인풋, 보더, 배경)만 BLP 규칙 적용. 콘텐츠(이미지, 영상, SVG, 3D, 차트)는 그라데이션·블러·풀컬러·애니메이션 다 허용.
- 이 디자인 언어는 다음에 모두 적용 가능:
  - 고밀도 정보 UI (대시보드, 채팅, 어드민, CRM)
  - e-commerce (쇼핑몰, 결제 플로우)
  - 마케팅 / 광고 (히어로 + 풀블리드 영상/이미지)
  - 모델 / 제품 리포트 (대형 카드 + 작은 통계 타일)
  - 도구 시연 (3D 뷰어, 물리엔진, 코드 에디터, 차트 빌더)
  - 문서 / 블로그 (본문 + 사이드바)
- 타일 크기/배치는 자유. 작은 격자부터 화면을 채우는 히어로 타일까지.
- 1000px 캡은 *본문 텍스트* 같은 좁은 콘텐츠용 기본값. 큰 히어로/풀블리드 섹션은 캡 없이 화면 폭 사용.
- "미니멀"의 정의: **둥글거나 각거나 둘 중 하나, 색은 BLP 안에서, 그림자는 샤프, 그라데이션은 *UI 크롬에 한해* 없음.**
  이 네 가지 제약이 *UI 크롬*의 모든 결정 필터. *콘텐츠*는 미니멀 필터 밖.
