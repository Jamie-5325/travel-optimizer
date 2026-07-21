import streamlit as st
import asyncio
import httpx
import itertools
from pydantic import BaseModel, Field, validator
from typing import List

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
        if isinstance(v, str):
            cleaned = v.replace(",", "").replace("₩", "").replace("KRW", "").strip()
            return int(cleaned) if cleaned else 0
        return v

class HotelModel(BaseModel):
    id: str = Field(description="호텔명")
    price: int = Field(gt=0, description="1박 가격")
    rating: float = Field(ge=0.0, le=5.0, default=3.0)

    @validator('price', pre=True)
    def clean_price(cls, v):
        if isinstance(v, str):
            cleaned = v.replace(",", "").replace("₩", "").strip()
            return int(cleaned) if cleaned else 0
        return v
        
    @validator('rating', pre=True)
    def validate_rating(cls, v):
        if v is None:
            return 3.0
        val = float(v)
        if val > 5.0: return 5.0
        if val < 0.0: return 0.0
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
try:
    SERPAPI_KEY = st.secrets["SERPAPI_KEY"]
except KeyError:
    SERPAPI_KEY = "로컬_테스트용_키를_여기에_입력"

async def fetch_flights_async(client, origin, destination, date):
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "currency": "KRW",
        "hl": "ko",
        "api_key": SERPAPI_KEY
    }
    try:
        response = await client.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        
        parsed = []
        flights_data = data.get("best_flights", []) + data.get("other_flights", [])
        
        for f in flights_data:
            if "price" not in f: continue
            flight_info = f.get("flights", [{}])[0]
            
            parsed.append({
                "id": f"{flight_info.get('flight_number', 'Unknown')}_{flight_info.get('airline', 'Unknown')}",
                "price": f.get("price", 0),
                "stops": len(f.get("layovers", [])),
                "duration": f.get("total_duration", 0) / 60.0
            })
        return validate_flights(parsed)
    except Exception as e:
        st.error(f"항공 API 에러: {e}")
        return []

async def fetch_hotels_async(client, destination, check_in, check_out):
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_hotels",
        "q": destination,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "currency": "KRW",
        "hl": "ko",
        "api_key": "0dd9533f83ac0823563e1edefb64ed54344c3f28ae6aee84c8779dfa8017133f"
        
    try:
        response = await client.get(url, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        
        parsed = []
        for p in data.get("properties", []):
            if "rate_per_night" not in p: continue
            parsed.append({
                "id": p.get("name", "Unknown Hotel"),
                "price": p.get("rate_per_night", {}).get("lowest", "0"),
                "rating": p.get("overall_rating", 3.0)
            })
        return validate_hotels(parsed)
    except Exception as e:
        st.error(f"숙박 API 에러: {e}")
        return []

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
    w1, w2, w3 = weights
    best_combo = None
    max_score = -float('inf')

    for flight, hotel in itertools.product(flights, hotels):
        total_price = flight['price'] + hotel['price']
        if total_price > budget: continue
            
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
            
            flights, hotels = get_cached_api_data(origin, destination, d_start, d_end)
            
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
