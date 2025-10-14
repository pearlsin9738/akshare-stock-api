# main.py (AKShare + 全优化版 3.0.3)
import os
import time
import akshare as ak
import pandas as pd
from datetime import datetime, time as dt_time
from typing import Optional, Tuple, List

from fastapi import FastAPI, Security, HTTPException, Request, status, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, Field
from cachetools import cached, TTLCache

# ----------- 基础配置 -----------
class Config:
    VERSION = "3.0.3"
    TITLE   = "A 股数据 API (AKShare 增强版)"
    DESC    = "提供实时行情与日线数据，含健康检查与交易时间提示"
    CONTACT = {"name": "YourName", "email": "you@example.com"}
    SERVER_URL = "https://akshare-stock-api.onrender.com"  # 添加服务器URL配置

# 创建不同数据源的缓存
realtime_cache = TTLCache(maxsize=100, ttl=60)   # 实时数据缓存1分钟
daily_cache = TTLCache(maxsize=200, ttl=3600)    # 日线数据缓存1小时

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title=Config.TITLE,
    description=Config.DESC,
    version=Config.VERSION,
    contact=Config.CONTACT,
    servers=[
        {
            "url": Config.SERVER_URL,
            "description": "生产环境"
        }
    ],  # 添加服务器配置
    docs_url="/docs",
    redoc_url="/redoc",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

security       = HTTPBearer()
BEARER_TOKEN   = os.getenv("MY_API_KEY", "default_secret")
START_TIME     = time.time()

# ----------- 数据模型 ----------
class StockQuote(BaseModel):
    """股票实时行情数据模型"""
    symbol: str       = Field(..., description="股票代码，如 000001.SZ")
    price: float      = Field(..., description="最新价")
    currency: str     = Field("CNY", description="货币类型")
    change_percent: float = Field(..., description="涨跌幅 %")
    trade_date: str   = Field(..., description="交易日期 YYYYMMDD")
    day_high: float   = Field(..., description="当日最高价")
    day_low: float    = Field(..., description="当日最低价")
    volume: float     = Field(..., description="成交量（手）")
    amount: float     = Field(..., description="成交额（千元）")
    warning: Optional[str] = Field(None, description="交易时间提示")

    class Config:
        json_schema_extra = {  # 修改为 json_schema_extra 避免警告
            "example": {
                "symbol": "000001.SZ",
                "price": 11.43,
                "currency": "CNY",
                "change_percent": 0.26,
                "trade_date": "20251010",
                "day_high": 11.5,
                "day_low": 11.3,
                "volume": 500000,
                "amount": 5715000,
                "warning": "非交易时间，数据可能延迟"
            }
        }


class DailyQuote(BaseModel):
    """股票日线数据模型（最近交易日）"""
    symbol: str       = Field(..., description="股票代码")
    close: float      = Field(..., description="收盘价")
    change_pct: float = Field(..., description="涨跌幅 %")
    trade_date: str   = Field(..., description="交易日期")

    class Config:
        json_schema_extra = {  # 修改为 json_schema_extra 避免警告
            "example": {"symbol": "000001.SZ", "close": 11.43, "change_pct": 0.26, "trade_date": "20251010"}
        }


class HealthCheck(BaseModel):
    """健康检查响应"""
    status: str   = Field(..., description="服务状态")
    timestamp: str = Field(..., description="服务器时间 ISO")
    version: str  = Field(..., description="API 版本")
    uptime: Optional[float] = Field(None, description="运行秒数")


# ----------- 工具函数 -----------
def is_holiday(date_str: str) -> bool:
    """简单节假日判断"""
    holidays = [
        "20250101", "20250102",  # 元旦
        "20250210", "20250211", "20250212", "20250213", "20250214",  # 春节
        "20250404", "20250405", "20250406",  # 清明节
        "20250501", "20250502", "20250503",  # 劳动节
        "20250608", "20250609", "20250610",  # 端午节
        "20250915", "20250916", "20250917",  # 中秋节
        "20251001", "20251002", "20251003", "20251004", "20251005", "20251006", "20251007"  # 国庆节
    ]
    return date_str in holidays

def is_trading_time() -> Tuple[bool, str]:
    """返回 (是否在交易时间, 原因)"""
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    weekday, ct = now.weekday(), now.time()
    
    # 检查节假日
    if is_holiday(date_str):
        return False, "节假日休市"
    
    if weekday >= 5:
        return False, "周末休市"
    
    morning = dt_time(9, 30) <= ct <= dt_time(11, 30)
    afternoon = dt_time(13, 0) <= ct <= dt_time(15, 0)
    
    if morning or afternoon:
        return True, "交易时间内"
    return False, "非交易时间"


@cached(realtime_cache)
def fetch_realtime(symbol: str):
    """AKShare 实时快照"""
    try:
        df = ak.stock_zh_a_spot_em()
        code = symbol.split(".")[0]          # 去掉后缀
        row = df[df["代码"] == code]
        return row.iloc[0] if not row.empty else None
    except Exception as e:
        print(f"获取实时数据错误: {e}")
        return None


@cached(daily_cache)
def fetch_daily(symbol: str):
    """AKShare 日线（最近交易日）"""
    try:
        code = symbol.split(".")[0]
        # 取最近交易日数据
        df = ak.stock_zh_a_hist(symbol=code, adjust="qfq")
        return df.iloc[-1] if not df.empty else None
    except Exception as e:
        print(f"获取日线数据错误: {e}")
        return None


# ----------- 接口 -----------
@app.get("/", tags=["系统"])
def root():
    """API 信息"""
    return {
        "message": "AKShare A 股 API 已启动",
        "version": Config.VERSION,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
        "server_url": Config.SERVER_URL,  # 添加服务器URL信息
        "endpoints": {
            "实时行情": "/get_stock_quote?symbol=000001.SZ",
            "日线行情": "/get_daily_quote?symbol=000001.SZ",
        },
    }


@app.get(
    "/health",
    response_model=HealthCheck,
    summary="服务健康检查",
    tags=["系统管理"],
)
async def health_check():
    """健康检查"""
    return HealthCheck(
        status="healthy",
        timestamp=datetime.now().isoformat(),
        version=Config.VERSION,
        uptime=round(time.time() - START_TIME, 2),
    )


@app.get(
    "/ready",
    summary="就绪检查",
    tags=["系统管理"],
)
async def readiness_check():
    """验证数据源是否可达"""
    try:
        test = ak.stock_zh_a_spot_em()
        if test.empty:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "reason": "AKShare 数据源空"}
            )
        return {"status": "ready", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "reason": str(e)}
        )


@app.get(
    "/get_stock_quote",
    response_model=StockQuote,
    summary="获取股票实时行情",
    description="获取实时价格、涨跌幅、成交量等；非交易时间仍会返回最近有效数据并提示",
    tags=["行情数据"],
)
@limiter.limit("20/minute")
async def get_stock_quote(
    request: Request,
    symbol: str = Query(..., description="股票代码，如 000001.SZ"),
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    if credentials.scheme != "Bearer" or credentials.credentials != BEARER_TOKEN:
        raise HTTPException(status_code=403, detail="无效的认证凭证")

    is_trading, time_reason = is_trading_time()
    row = fetch_realtime(symbol)
    if row is None:
        raise HTTPException(status_code=404, detail=f"AKShare 找不到 {symbol}")

    try:
        price = float(row["最新价"])
        pre_close = float(row["昨收"])
        change_pct = ((price - pre_close) / pre_close) * 100 if pre_close else 0
        
        # 构建响应
        response_data = StockQuote(
            symbol=symbol,
            price=price,
            currency="CNY",
            change_percent=round(change_pct, 2),
            trade_date=datetime.now().strftime("%Y%m%d"),
            day_high=float(row["最高"]),
            day_low=float(row["最低"]),
            volume=float(row["成交量"]) / 100,
            amount=float(row["成交额"]) / 1000,
        )
        
        # 添加交易时间提示
        if not is_trading:
            response_data.warning = time_reason
        
        return response_data
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=500, detail=f"解析数据失败: {e}")


@app.get(
    "/get_daily_quote",
    response_model=DailyQuote,
    summary="获取股票日线数据（最近交易日）",
    tags=["行情数据"],
)
@limiter.limit("20/minute")
async def get_daily_quote(
    request: Request,
    symbol: str = Query(..., description="股票代码，如 000001.SZ"),
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    if credentials.scheme != "Bearer" or credentials.credentials != BEARER_TOKEN:
        raise HTTPException(status_code=403, detail="无效的认证凭证")

    row = fetch_daily(symbol)
    if row is None:
        raise HTTPException(status_code=404, detail=f"AKShare 找不到 {symbol} 日线数据")

    try:
        # 处理日期字段
        trade_date = row["日期"]
        if isinstance(trade_date, pd.Timestamp):
            trade_date = trade_date.strftime("%Y%m%d")
        elif isinstance(trade_date, str):
            trade_date = trade_date.replace("-", "")[:8]
        else:
            trade_date = datetime.now().strftime("%Y%m%d")

        return DailyQuote(
            symbol=symbol,
            close=float(row["收盘"]),
            change_pct=float(row["涨跌幅"]),
            trade_date=trade_date,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理日线数据失败: {e}")