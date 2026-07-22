import streamlit as st
import asyncio
import httpx
import itertools
import re
from datetime import date, timedelta
from urllib.parse import quote
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Tuple

# ==========================================
# 0. 공통 유틸리티
# ==========================================
def clean_currency_string(v):
    """
    가격 문자열에서 통화 기호/구분자를 제거하고 정수로 변환.
    FlightModel, HotelModel이 동일한 로직을 공유하도록 통합
    (기존 코드는 HotelModel에서 'KRW' 문자열을 제거하지 않아
     "150,000 KRW" 같은 값이 들어오면 파싱에 실패하는 버그가 있었음).
    """
    if isinstance(v, str):
        cleaned = (
            v.replace(",", "")
             .replace("₩", "")
             .replace("KRW", "")
             .replace("원", "")
             .strip()
        )
        return int(float(cleaned)) if cleaned else 0
    return v


def format_korean_won(amount: int) -> str:
    """
    숫자를 억/만 단위의 한글 표기로 변환한다.
    예: 1,000,000 -> '100만원', 123,456,789 -> '1억 2,345만 6,789원'
    """
    amount = int(amount)
    if amount == 0:
        return "0원"

    eok, rem = divmod(amount, 100_000_000)
    man, won = divmod(rem, 10_000)

    parts = []
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    if won or not parts:
        parts.append(f"{won:,}")
    return " ".join(parts) + "원"


# ==========================================
# 1. Pydantic 데이터 검증 모델
# ==========================================
class FlightModel(BaseModel):
    id: str = Field(description="항공편 고유 ID")
    price: int = Field(gt=0, description="왕복 항공권 예상 가격")
    stops: int = Field(ge=0, description="출국편 경유 횟수")
    duration: float = Field(gt=0.0, description="출국편 비행 시간")
    link: Optional[str] = Field(default=None, description="항공편 예약/조회 링크")

    @validator('price', pre=True)
    def clean_price(cls, v):
        return clean_currency_string(v)


class HotelModel(BaseModel):
    id: str = Field(description="호텔명")
    price: int = Field(gt=0, description="1박 가격")
    rating: float = Field(ge=0.0, le=5.0, default=3.0)
    link: Optional[str] = Field(default=None, description="호텔 예약/상세 페이지 링크")

    @validator('price', pre=True)
    def clean_price(cls, v):
        return clean_currency_string(v)

    @validator('rating', pre=True)
    def validate_rating(cls, v):
        if v is None:
            return 3.0
        val = float(v)
        if val > 5.0:
            return 5.0
        if val < 0.0:
            return 0.0
        return val


def validate_flights(raw_data: List[dict]) -> List[dict]:
    validated = []
    for raw in raw_data:
        try:
            validated.append(FlightModel(**raw).model_dump())
        except Exception:
            continue
    return validated


def validate_hotels(raw_data: List[dict]) -> List[dict]:
    validated = []
    for raw in raw_data:
        try:
            validated.append(HotelModel(**raw).model_dump())
        except Exception:
            continue
    return validated

# ==========================================
# 2. 비동기 API 통신 모듈
# ==========================================
# 수정: 시크릿 파일 자체가 없는 경우 Streamlit은 KeyError가 아니라
# FileNotFoundError를 던진다. 기존 코드는 KeyError만 잡고 있어서
# secrets.toml이 아예 없는 로컬 환경에서는 앱이 그대로 죽는 버그가 있었음.
try:
    SERPAPI_KEY = st.secrets["SERPAPI_KEY"]
except (KeyError, FileNotFoundError):
    SERPAPI_KEY = "로컬_테스트용_키를_여기에_입력"


@st.cache_data(ttl=60 * 60 * 24)  # 공항/도시 매핑은 자주 바뀌지 않으므로 24시간 캐시
def _fetch_autocomplete_raw(query: str, hl: str = "ko") -> Tuple[List[dict], Optional[str]]:
    """SerpApi Google Flights Autocomplete의 원본 suggestions 목록을 가져온다.
    resolve_flight_location(단일 픽)과 search_location_candidates(여러 후보 리스트)가
    이 함수를 공유해서 쓴다."""
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights_autocomplete",
        "q": query,
        "hl": hl,
        "api_key": SERPAPI_KEY
    }

    async def _fetch():
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=15.0)
            response.raise_for_status()
            return response.json()

    try:
        data = asyncio.run(_fetch())
    except Exception as e:
        return [], f"'{query}' 위치 조회 실패: {e}"

    if "error" in data:
        return [], f"'{query}' 위치 조회 오류: {data['error']}"

    suggestions = data.get("suggestions", [])
    if not suggestions:
        return [], f"'{query}'에 해당하는 공항/도시를 찾을 수 없습니다."
    return suggestions, None


def resolve_flight_location(query: str, hl: str = "ko") -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    출발지/도착지 입력값을 항공권 조회(departure_id/arrival_id)에 쓸 수 있는
    값으로 변환한다.
    - 3자리 영문 공항코드(예: ICN, NRT)는 그대로 사용
    - 그 외 도시/국가명(예: 서울, Tokyo)은 SerpApi Google Flights Autocomplete
      API(engine=google_flights_autocomplete)로 조회해, 해당 도시의 kgmid
      (예: /m/0hsqf)를 사용한다. 도시 kgmid를 쓰면 그 도시의 모든 공항이
      자동으로 포함되어 특정 공항 하나를 임의로 고르지 않아도 된다.

    반환값: (resolved_id, 인식된 이름(표시용), 오류 메시지)
    """
    q = query.strip()
    if not q:
        return None, None, "출발지/도착지를 입력해주세요."

    if re.fullmatch(r"[A-Za-z]{3}", q):
        code = q.upper()
        return code, code, None

    suggestions, err = _fetch_autocomplete_raw(q, hl)
    if err:
        return None, None, err

    # 도시 단위 결과를 우선한다(해당 도시의 모든 공항을 포함하는 kgmid 사용).
    # 없으면 최상단 추천 결과를 그대로 사용한다.
    chosen = next((s for s in suggestions if s.get("type") == "city" and s.get("id")), None)
    chosen = chosen or suggestions[0]
    resolved_id = chosen.get("id")
    resolved_name = chosen.get("name", q)

    if not resolved_id:
        return None, None, f"'{query}'의 위치 코드를 확인할 수 없습니다."
    return resolved_id, resolved_name, None


def search_location_candidates(query: str, hl: str = "ko") -> Tuple[List[dict], Optional[str]]:
    """
    출발지/도착지 입력창 옆 '후보 목록' 팝업에 쓸 다중 후보 리스트를 반환한다.
    resolve_flight_location과 달리 하나로 좁히지 않고, 도시 단위 결과와 그
    하위 공항들을 모두 펼쳐서 사용자가 직접 고를 수 있게 한다.
    각 후보는 {"id", "name", "type"} 딕셔너리.
    """
    q = query.strip()
    if not q:
        return [], "검색어를 입력해주세요."

    if re.fullmatch(r"[A-Za-z]{3}", q):
        code = q.upper()
        return [{"id": code, "name": code, "type": "airport"}], None

    suggestions, err = _fetch_autocomplete_raw(q, hl)
    if err:
        return [], err

    candidates = []
    for s in suggestions:
        if s.get("type") == "city" and s.get("id"):
            candidates.append({"id": s["id"], "name": s.get("name", q), "type": "city"})
            for a in s.get("airports", []):
                if a.get("id"):
                    candidates.append({
                        "id": a["id"],
                        "name": f"{a.get('name', a['id'])} ({a['id']})",
                        "type": "airport"
                    })
        elif s.get("id"):
            candidates.append({"id": s["id"], "name": s.get("name", q), "type": s.get("type", "region")})

    return candidates[:8], None


async def fetch_flights_async(client, origin_id, destination_id, outbound_date, return_date, adults, children, infants, flights_link) -> Tuple[List[dict], Optional[str]]:
    """
    왕복(Round trip) 항공권을 조회한다.

    참고: SerpApi google_flights 엔진은 초기 왕복 검색 결과(best_flights/
    other_flights)에 담긴 'price'가 이미 Google Flights가 보여주는 왕복
    예상 요금이다(Google Flights UI에서 출발 항공편을 고르기 전에 보이는
    가격과 동일한 성격). 다만 이는 "예상" 가격이며, 실제로 특정 귀국편까지
    확정한 확정 가격을 받으려면 이 결과의 departure_token으로 귀국편을
    다시 조회하고, 그 결과의 booking_token으로 최종 예약 옵션을 조회하는
    2~3단계 추가 요청이 필요하다(SerpApi 공식 문서 기준).
    이 앱은 여러 항공편 x 여러 호텔 조합을 비교하는 최적화 도구라서,
    후보 하나하나에 대해 추가 조회를 하면 API 호출 수가 매우 커진다.
    따라서 여기서는 1단계 조회의 예상 왕복 가격만 사용하고, 실제 예약
    가능 여부/최종 가격은 Google Flights 링크에서 직접 확인하도록 안내한다.

    origin_id/destination_id는 resolve_flight_location()이 반환한, 이미
    공항코드 또는 kgmid로 변환된 값이어야 한다.
    """
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights",
        "departure_id": origin_id,
        "arrival_id": destination_id,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "type": "1",  # 왕복
        "adults": adults,
        "children": children,
        "infants_on_lap": min(infants, adults),  # 유아(무릎 동반)는 보호자 1인당 1명까지
        "currency": "KRW",
        "hl": "ko",
        "api_key": SERPAPI_KEY
    }
    try:
        response = await client.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            return [], f"항공권 API 오류: {data['error']}"

        parsed = []
        flights_data = data.get("best_flights", []) + data.get("other_flights", [])

        for f in flights_data:
            if "price" not in f:
                continue
            flight_info = f.get("flights", [{}])[0]

            # 왕복 검색의 1단계 응답에는 출국편 정보만 담기므로(귀국편은
            # departure_token으로 별도 조회해야 함), stops/duration은
            # 출국편 기준 값이다. price만 Google이 제공하는 왕복 예상 가격.
            parsed.append({
                "id": f"{flight_info.get('flight_number', 'Unknown')}_{flight_info.get('airline', 'Unknown')}",
                "price": f.get("price", 0),
                "stops": len(f.get("layovers", [])),
                "duration": f.get("total_duration", 0) / 60.0,
                "link": flights_link
            })
        return validate_flights(parsed), None
    except httpx.HTTPStatusError as e:
        return [], f"항공권 API 응답 오류 (HTTP {e.response.status_code})"
    except Exception as e:
        return [], f"항공권 조회 실패: {e}"


async def fetch_hotels_async(client, destination, check_in, check_out, adults, children) -> Tuple[List[dict], Optional[str]]:
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_hotels",
        "q": destination,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "adults": adults,
        "children": children,
        "currency": "KRW",
        "hl": "ko",
        "api_key": SERPAPI_KEY
    }
    if children > 0:
        # Google Hotels는 children > 0이면 children_ages(나이별 목록)가 필요하다.
        # 이 앱은 소아 인원수만 받으므로, 대표 나이(8세)를 인원수만큼 채워 넣는다.
        params["children_ages"] = ",".join(["8"] * children)
    try:
        response = await client.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            return [], f"숙박 API 오류: {data['error']}"

        parsed = []
        for p in data.get("properties", []):
            if "rate_per_night" not in p:
                continue
            hotel_name = p.get("name", "Unknown Hotel")
            # 공식 홈페이지 link가 없는 프로퍼티(특히 소규모/무브랜드 숙소)도
            # 있어, 이 경우 Google Hotels 검색 링크로 대체해 항상 클릭 가능한
            # 링크를 보장한다.
            hotel_link = p.get("link") or (
                "https://www.google.com/travel/hotels?q="
                + quote(f"{hotel_name} {destination}")
            )
            parsed.append({
                "id": hotel_name,
                "price": p.get("rate_per_night", {}).get("lowest", "0"),
                "rating": p.get("overall_rating", 3.0),
                "link": hotel_link
            })
        return validate_hotels(parsed), None
    except httpx.HTTPStatusError as e:
        return [], f"숙박 API 응답 오류 (HTTP {e.response.status_code})"
    except Exception as e:
        return [], f"숙박 조회 실패: {e}"


async def fetch_all_data(origin_id, destination_id, destination_query, check_in, check_out, adults, children, infants, flights_link):
    async with httpx.AsyncClient() as client:
        # check_in = 출국일(outbound_date), check_out = 귀국일(return_date) 겸
        # 호텔 체크아웃일로 동일하게 사용한다(왕복 여행 = 숙박 기간과 동일).
        f_task = fetch_flights_async(client, origin_id, destination_id, check_in, check_out, adults, children, infants, flights_link)
        h_task = fetch_hotels_async(client, destination_query, check_in, check_out, adults, children)
        return await asyncio.gather(f_task, h_task)


@st.cache_data(ttl=3600)
def get_cached_api_data(origin_id, destination_id, destination_query, check_in, check_out, adults, children, infants, flights_link):
    return asyncio.run(fetch_all_data(origin_id, destination_id, destination_query, check_in, check_out, adults, children, infants, flights_link))

# ==========================================
# 3. 최적화 알고리즘
# ==========================================
def find_optimal_combination(flights, hotels, budget, weights):
    """
    참고: w1(예산 소진율 비중)은 (총가격 / 예산) 비율에 곱해져
    '예산을 많이 쓸수록' 점수가 올라가는 구조다. 저렴한 조합을 우대하는
    일반적인 '최저가 최적화'와는 반대 방향이므로, 의도한 설계인지 확인이
    필요하다(요청 시 부호를 반대로 바꿀 수 있음). 이번 수정에서는
    로직을 임의로 바꾸지 않고 그대로 유지했다.

    hotels의 각 항목은 'total_price'(숙박 전체 기간 총액) 키를 포함해야
    한다. 'price'는 1박 요금(표시용)이라 총비용 계산에는 쓰지 않는다.
    """
    w1, w2, w3 = weights
    best_combo = None
    max_score = -float('inf')

    for flight, hotel in itertools.product(flights, hotels):
        total_price = flight['price'] + hotel['total_price']
        if total_price > budget:
            continue

        score = (w1 * (total_price / budget)) + (w2 * (hotel['rating'] / 5.0)) - (w3 * (flight['stops'] * 0.15))

        if score > max_score:
            max_score = score
            best_combo = {
                'flight': flight,
                'hotel': hotel,
                'total_price': total_price,
                'score': score
            }
    return best_combo


# ==========================================
# 3-1. UI 헬퍼 (위치 팝업 선택 / 여행지 추천 적용)
# ==========================================
# 후보 목록 팝업에서 검색어가 비어 있을 때 기본으로 보여줄 글로벌 주요 도시.
# id는 공항코드라 바로 3자리 코드 매칭 경로를 타서 추가 조회 없이 빠르다.
MAJOR_CITIES = [
    {"name": "서울", "id": "ICN"},
    {"name": "도쿄", "id": "NRT"},
    {"name": "오사카", "id": "KIX"},
    {"name": "방콕", "id": "BKK"},
    {"name": "싱가포르", "id": "SIN"},
    {"name": "홍콩", "id": "HKG"},
    {"name": "타이베이", "id": "TPE"},
    {"name": "다낭", "id": "DAD"},
    {"name": "파리", "id": "CDG"},
    {"name": "런던", "id": "LHR"},
    {"name": "로마", "id": "FCO"},
    {"name": "바르셀로나", "id": "BCN"},
    {"name": "뉴욕", "id": "JFK"},
    {"name": "로스앤젤레스", "id": "LAX"},
    {"name": "두바이", "id": "DXB"},
    {"name": "시드니", "id": "SYD"},
]


def _set_location_value(state_key: str, value: str):
    """팝업 후보 버튼 클릭 시 콜백으로 호출되어, 텍스트 입력값을 갱신한다.
    콜백은 스크립트가 다시 실행되기 전에 처리되므로, 이미 렌더링된 위젯의
    session_state를 직접 바꿔도 충돌 없이 안전하다."""
    st.session_state[state_key] = value


def location_picker(label: str, state_key: str, default: str) -> str:
    """텍스트 입력 + '후보 목록 보기' 팝업으로 출발지/도착지를 고르는 위젯.
    입력창이 비어 있으면 글로벌 주요 도시 목록을, 검색어가 있으면 자동완성
    검색 결과를 보여준다. 팝업 안에서 항목을 클릭하면 입력창 값이 바로 그
    항목으로 바뀐다."""
    if state_key not in st.session_state:
        st.session_state[state_key] = default

    st.text_input(label, key=state_key)

    with st.popover("🔍 후보 목록 보기", use_container_width=True, key=f"{state_key}_popover"):
        query = st.session_state.get(state_key, "")
        if not query.strip():
            st.caption("🌍 글로벌 주요 도시")
            for c in MAJOR_CITIES:
                st.button(
                    f"🏙️ {c['name']} ({c['id']})",
                    key=f"{state_key}_major_{c['id']}",
                    use_container_width=True,
                    on_click=_set_location_value,
                    args=(state_key, c["id"])
                )
        else:
            candidates, err = search_location_candidates(query)
            if err:
                st.caption(f"⚠️ {err}")
            elif not candidates:
                st.caption("일치하는 후보가 없습니다.")
            else:
                for c in candidates:
                    badge = "🏙️" if c["type"] == "city" else ("✈️" if c["type"] == "airport" else "🌍")
                    st.button(
                        f"{badge} {c['name']}",
                        key=f"{state_key}_pick_{c['id']}",
                        use_container_width=True,
                        on_click=_set_location_value,
                        args=(state_key, c["name"])
                    )

    return st.session_state[state_key]


# ==========================================
# 4. 모바일 최적화 Streamlit UI
# ==========================================
st.set_page_config(page_title="항공+숙박 최적화", layout="centered", initial_sidebar_state="collapsed")

# 주의: 아래 HTML 문자열은 각 줄을 반드시 들여쓰기 없이(맨 앞 칸부터) 작성해야
# 한다. Markdown은 줄 앞에 공백이 4칸 이상이면 "코드 블록"으로 인식해버려서,
# unsafe_allow_html=True를 줘도 HTML/SVG가 그대로 렌더링되지 않고 텍스트로
# 표시(또는 미표시)되는 문제가 있었다.
illustration_html = """<div style="text-align:center; margin: 0.5rem 0 0.75rem 0;">
<svg viewBox="0 0 400 200" xmlns="http://www.w3.org/2000/svg" style="width:100%; max-width:360px; height:auto;">
<defs>
<linearGradient id="skyGrad" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="#EAF4F8"/>
<stop offset="100%" stop-color="#F7EFE3"/>
</linearGradient>
</defs>
<rect x="0" y="0" width="400" height="200" rx="24" fill="url(#skyGrad)"/>
<path d="M70,150 C150,60 250,50 330,70" fill="none" stroke="#8FB9C9" stroke-width="3" stroke-dasharray="2 10" stroke-linecap="round"/>
<g transform="translate(48,150)">
<rect x="-24" y="-14" width="48" height="34" rx="6" fill="#E4784B"/>
<rect x="-9" y="-24" width="18" height="12" rx="3" fill="#C85F36"/>
<rect x="-24" y="-2" width="48" height="6" fill="#C85F36"/>
</g>
<g transform="translate(230,68) rotate(-18)">
<path d="M-16,-6 L16,0 L-16,6 L-6,0 Z" fill="#2E5E73"/>
</g>
<g transform="translate(335,68)">
<path d="M0,-16 C9,-16 16,-9 16,0 C16,11 0,26 0,26 C0,26 -16,11 -16,0 C-16,-9 -9,-16 0,-16 Z" fill="#3E7C8C"/>
<circle cx="0" cy="0" r="5.5" fill="#FFFFFF"/>
</g>
</svg>
</div>
<p style="text-align:center; font-size:1.25rem; font-weight:700; color:#2E5E73; line-height:1.4; margin: 0 0 1rem 0;">
항공권과 숙소, 정해진 예산 안에서<br>가장 완벽한 조합을 찾아드립니다.
</p>"""

st.markdown(illustration_html, unsafe_allow_html=True)

# --- 최상단: 출발지 + 예산 (항상 보이는 영역) ---
origin = location_picker("출발지 (공항코드 또는 도시명)", "origin_text", "ICN")
st.caption("예: ICN, 서울, Seoul 모두 입력 가능합니다. 🔍 버튼을 누르면 후보 목록이 팝업으로 뜹니다.")



def _sync_budget_from_text():
    """입력창의 텍스트(콤마 포함 가능)를 정수로 변환해 실제 예산 값으로
    저장하고, 입력창 표시값은 콤마가 포함된 형태로 다시 맞춰준다."""
    raw = st.session_state.get("budget_text", "")
    cleaned = raw.replace(",", "").replace("원", "").strip()
    try:
        value = int(cleaned) if cleaned else 100000
    except ValueError:
        value = st.session_state.get("budget_value", 1000000)
    value = max(value, 100000)
    st.session_state["budget_value"] = value
    st.session_state["budget_text"] = f"{value:,}"


if "budget_value" not in st.session_state:
    st.session_state["budget_value"] = 1000000
if "budget_text" not in st.session_state:
    st.session_state["budget_text"] = f"{st.session_state['budget_value']:,}"

st.text_input(
    "최대 예산 (원)",
    key="budget_text",
    on_change=_sync_budget_from_text,
    help="숫자만 입력해도 자동으로 콤마(,)가 붙습니다. 최소 100,000원."
)
budget = st.session_state["budget_value"]
st.caption(f"💰 {budget:,}원 ({format_korean_won(budget)})")

if "pax_adults" not in st.session_state:
    st.session_state["pax_adults"] = 1
if "pax_children" not in st.session_state:
    st.session_state["pax_children"] = 0
if "pax_infants" not in st.session_state:
    st.session_state["pax_infants"] = 0

_pax_summary = f"성인 {st.session_state['pax_adults']}명"
if st.session_state["pax_children"] > 0:
    _pax_summary += f" · 소아 {st.session_state['pax_children']}명"
if st.session_state["pax_infants"] > 0:
    _pax_summary += f" · 유아 {st.session_state['pax_infants']}명"

with st.popover(f"👤 {_pax_summary}", use_container_width=True):
    adults = st.number_input("성인", min_value=1, step=1, key="pax_adults")
    children = st.number_input("소아 (만 2~11세)", min_value=0, step=1, key="pax_children")
    infants = st.number_input("유아 (만 2세 미만)", min_value=0, step=1, key="pax_infants")
    if infants > adults:
        st.caption(f"ℹ️ 유아는 보호자(성인) 1명당 1명까지 동반할 수 있어, 조회 시 {adults}명까지만 반영됩니다.")
    if children > 0:
        st.caption("ℹ️ 소아 나이는 편의상 대표 나이(8세)로 계산됩니다. 정확한 나이별 요금은 예약 시 다시 확인해주세요.")

st.divider()

# --- 세부 검색 조건 ---
with st.expander("🔍 세부 검색 조건 및 가중치 설정", expanded=True):
    destination = location_picker("도착지 (공항코드 또는 도시명)", "destination_text", "NRT")
    st.caption("예: NRT, 도쿄, Tokyo 모두 입력 가능합니다. 🔍 버튼을 누르면 후보 목록이 팝업으로 뜹니다.")

    if "start_date_input" not in st.session_state:
        st.session_state["start_date_input"] = date.today()
    if "end_date_input" not in st.session_state:
        st.session_state["end_date_input"] = date.today() + timedelta(days=7)

    col_date1, col_date2 = st.columns(2)
    start_date = col_date1.date_input("출발일", key="start_date_input")
    end_date = col_date2.date_input("귀국일", key="end_date_input")
    st.caption("ℹ️ 항공권은 왕복(출발일→귀국일) 기준으로 조회됩니다. 표시되는 가격은 Google Flights의 왕복 예상 요금이며, 실제 예약 가능 여부와 최종 가격은 링크에서 다시 확인해주세요.")

    st.divider()
    st.caption("ℹ️ '예산 소진율 비중'을 높일수록 예산 한도 내에서 총비용이 예산에 최대한 가깝게 나오도록 우선순위를 둡니다(예산 초과는 항상 차단됩니다).")
    w_budget = st.slider("예산 소진율 비중", 0.0, 1.0, 1.0)
    w_hotel = st.slider("숙박 품질 비중", 0.0, 1.0, 0.3)
    w_flight = st.slider("직항 선호 비중", 0.0, 1.0, 0.8)

if st.button("최적 조합 검색", use_container_width=True):
    if start_date >= end_date:
        st.error("귀국일은 출발일 이후여야 합니다.")
    else:
        with st.spinner("출발지/도착지 확인 중..."):
            origin_id, origin_name, origin_err = resolve_flight_location(origin)
            dest_id, dest_name, dest_err = resolve_flight_location(destination)

        if origin_err or dest_err:
            if origin_err:
                st.error(f"출발지 인식 실패: {origin_err}")
            if dest_err:
                st.error(f"도착지 인식 실패: {dest_err}")
        else:
            st.caption(f"✓ 출발지: {origin_name} · 도착지: {dest_name} · 성인 {adults}명 · 소아 {children}명 · 유아 {min(infants, adults)}명")

            with st.spinner("데이터 통신 및 최적화 계산 중..."):
                d_start = start_date.strftime("%Y-%m-%d")
                d_end = end_date.strftime("%Y-%m-%d")

                # 항공편별 개별 예약 링크는 departure_token/booking_token을 이용한
                # 별도 2~3단계 요청이 필요해 현재 단일 요청 구조와는 맞지 않는다.
                # 대신 인식된 지명 + 왕복 날짜로 Google Flights 자연어 검색(q=)
                # 링크를 만들어 항상 유효한 딥링크를 보장한다.
                flights_link = (
                    "https://www.google.com/travel/flights?q="
                    + quote(f"Round trip flights from {origin_name} to {dest_name}, depart {d_start} return {d_end}")
                )

                (flights, f_err), (hotels, h_err) = get_cached_api_data(
                    origin_id, dest_id, destination, d_start, d_end, adults, children, infants, flights_link
                )

                if f_err:
                    st.warning(f_err)
                if h_err:
                    st.warning(h_err)

                if not flights or not hotels:
                    st.error("조건을 충족하는 데이터가 없거나 API 응답에 실패했습니다.")
                else:
                    nights = (end_date - start_date).days
                    # hotel['price']는 API가 주는 1박 요금이므로, 실제 예산과
                    # 비교하려면 숙박 일수만큼 곱한 전체 숙박비가 필요하다.
                    hotels_priced = [
                        {**h, "price_per_night": h["price"], "total_price": h["price"] * nights}
                        for h in hotels
                    ]

                    result = find_optimal_combination(
                        flights, hotels_priced, budget, weights=(w_budget, w_hotel, w_flight)
                    )

                    if result:
                        st.success(f"탐색 완료 (항공 {len(flights)}건, 숙박 {len(hotels)}건, {nights}박 기준)")

                        col1, col2 = st.columns(2)
                        col1.metric("총 소요 비용", f"{result['total_price']:,} 원")
                        col2.metric("잔여 예산", f"{budget - result['total_price']:,} 원")
                        st.metric("유틸리티 점수", f"{result['score']:.2f}")

                        st.divider()
                        flight = result['flight']
                        hotel = result['hotel']

                        link_col1, link_col2 = st.columns(2)

                        with link_col1:
                            st.markdown("**✈️ 추천 항공편 (왕복)**")
                            st.write(f"{flight['id']}")
                            st.write(f"왕복 {flight['price']:,}원 · 출국편 경유 {flight['stops']}회 · 출국편 비행 {flight['duration']:.1f}시간")
                            if flight.get('link'):
                                st.link_button("Google Flights에서 보기", flight['link'], use_container_width=True)
                            else:
                                st.caption("예약 링크를 찾을 수 없습니다.")

                        with link_col2:
                            st.markdown(f"**🏨 추천 숙소 ({nights}박)**")
                            st.write(f"{hotel['id']}")
                            st.write(f"1박 {hotel['price_per_night']:,}원 × {nights}박 = 총 {hotel['total_price']:,}원 · 평점 {hotel['rating']:.1f}")
                            if hotel.get('link'):
                                st.link_button("호텔 페이지에서 보기", hotel['link'], use_container_width=True)
                            else:
                                st.caption("예약 링크를 찾을 수 없습니다.")

                        with st.expander("상세 데이터 (JSON)"):
                            st.json({"항공편": flight, "숙박": hotel})
                    else:
                        st.error("해당 예산으로 구성 가능한 조합이 없습니다.")
