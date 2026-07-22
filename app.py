import streamlit as st
import asyncio
import httpx
import itertools
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


# ==========================================
# 1. Pydantic 데이터 검증 모델
# ==========================================
class FlightModel(BaseModel):
    id: str = Field(description="항공편 고유 ID")
    price: int = Field(gt=0, description="항공권 가격")
    stops: int = Field(ge=0, description="경유 횟수")
    duration: float = Field(gt=0.0, description="비행 시간")

    @validator('price', pre=True)
    def clean_price(cls, v):
        return clean_currency_string(v)


class HotelModel(BaseModel):
    id: str = Field(description="호텔명")
    price: int = Field(gt=0, description="1박 가격")
    rating: float = Field(ge=0.0, le=5.0, default=3.0)

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


async def fetch_flights_async(client, origin, destination, date) -> Tuple[List[dict], Optional[str]]:
    """
    편도(One-way) 항공권을 조회한다.
    참고: SerpApi google_flights 엔진은 'type' 파라미터 기본값이
    1(왕복)이며, 왕복인 경우 return_date가 필수라 이 파라미터 없이 호출하면
    API가 오류를 반환한다(기존 코드의 실질적 버그). 왕복 검색을 지원하려면
    departure_token을 이용한 2단계 조회가 별도로 필요하므로, 여기서는
    명시적으로 편도(type=2)로 고정해 단일 요청 구조를 유지한다.
    """
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "type": "2",  # 편도. 왕복(1)은 return_date 필수 + departure_token 2단계 조회 필요
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

            parsed.append({
                "id": f"{flight_info.get('flight_number', 'Unknown')}_{flight_info.get('airline', 'Unknown')}",
                "price": f.get("price", 0),
                "stops": len(f.get("layovers", [])),
                "duration": f.get("total_duration", 0) / 60.0
            })
        return validate_flights(parsed), None
    except httpx.HTTPStatusError as e:
        return [], f"항공권 API 응답 오류 (HTTP {e.response.status_code})"
    except Exception as e:
        return [], f"항공권 조회 실패: {e}"


async def fetch_hotels_async(client, destination, check_in, check_out) -> Tuple[List[dict], Optional[str]]:
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_hotels",
        "q": destination,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "currency": "KRW",
        "hl": "ko",
        "api_key": SERPAPI_KEY
    }
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
            parsed.append({
                "id": p.get("name", "Unknown Hotel"),
                "price": p.get("rate_per_night", {}).get("lowest", "0"),
                "rating": p.get("overall_rating", 3.0)
            })
        return validate_hotels(parsed), None
    except httpx.HTTPStatusError as e:
        return [], f"숙박 API 응답 오류 (HTTP {e.response.status_code})"
    except Exception as e:
        return [], f"숙박 조회 실패: {e}"


async def fetch_all_data(origin, destination, check_in, check_out):
    async with httpx.AsyncClient() as client:
        f_task = fetch_flights_async(client, origin, destination, check_in)
        h_task = fetch_hotels_async(client, destination, check_in, check_out)
        return await asyncio.gather(f_task, h_task)


@st.cache_data(ttl=3600)
def get_cached_api_data(origin, destination, check_in, check_out):
    return asyncio.run(fetch_all_data(origin, destination, check_in, check_out))

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
    """
    w1, w2, w3 = weights
    best_combo = None
    max_score = -float('inf')

    for flight, hotel in itertools.product(flights, hotels):
        total_price = flight['price'] + hotel['price']
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
# 4. 모바일 최적화 Streamlit UI
# ==========================================
st.set_page_config(page_title="항공+숙박 최적화", layout="centered", initial_sidebar_state="collapsed")
st.title("✈️ 여행 예산 최적화")

with st.expander("🔍 검색 조건 및 가중치 설정", expanded=True):
    col_loc1, col_loc2 = st.columns(2)
    origin = col_loc1.text_input("출발지 (코드)", value="ICN")
    destination = col_loc2.text_input("도착지 (코드)", value="NRT")

    col_date1, col_date2 = st.columns(2)
    start_date = col_date1.date_input("출발일")
    end_date = col_date2.date_input("도착일")
    st.caption("ℹ️ 항공권은 '출발일' 기준 편도로 조회되며, '도착일'은 숙박 체크아웃 날짜로만 사용됩니다.")

    budget = st.number_input("최대 예산 (원)", min_value=100000, value=1000000, step=50000)

    st.divider()
    w_budget = st.slider("예산 소진율 비중", 0.0, 1.0, 0.5)
    w_hotel = st.slider("숙박 품질 비중", 0.0, 1.0, 0.3)
    w_flight = st.slider("직항 선호 비중", 0.0, 1.0, 0.2)

if st.button("최적 조합 검색", use_container_width=True):
    if start_date >= end_date:
        st.error("도착일은 출발일 이후여야 합니다.")
    else:
        with st.spinner("데이터 통신 및 최적화 계산 중..."):
            d_start = start_date.strftime("%Y-%m-%d")
            d_end = end_date.strftime("%Y-%m-%d")

            (flights, f_err), (hotels, h_err) = get_cached_api_data(origin, destination, d_start, d_end)

            if f_err:
                st.warning(f_err)
            if h_err:
                st.warning(h_err)

            if not flights or not hotels:
                st.error("조건을 충족하는 데이터가 없거나 API 응답에 실패했습니다.")
            else:
                result = find_optimal_combination(
                    flights, hotels, budget, weights=(w_budget, w_hotel, w_flight)
                )

                if result:
                    st.success(f"탐색 완료 (항공 {len(flights)}건, 숙박 {len(hotels)}건)")

                    col1, col2 = st.columns(2)
                    col1.metric("총 소요 비용", f"{result['total_price']:,} 원")
                    col2.metric("잔여 예산", f"{budget - result['total_price']:,} 원")
                    st.metric("유틸리티 점수", f"{result['score']:.2f}")

                    st.json({"항공편": result['flight'], "숙박": result['hotel']})
                else:
                    st.error("해당 예산으로 구성 가능한 조합이 없습니다.")
