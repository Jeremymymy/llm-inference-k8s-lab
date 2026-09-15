"""pricer-api —— 估價服務的對外門面。

它取代課程 week8 裡直接呼叫 Modal 的 SpecialistAgent。原版是這樣：

    Pricer = modal.Cls.from_name("pricer-service", "Pricer")
    result = self.pricer.price.remote(description)   # 沒有 timeout、沒有 retry

換成 HTTP 之後這兩個都必須自己補，否則後端 pod 一重啟，呼叫端就整條炸掉。

職責刻意切得很薄：
  - 組 prompt（模板必須與訓練時逐字相同）
  - 打後端的 /v1/completions（透過 k8s Service DNS）
  - 解析價格、處理逾時與重試
真正的推論在另一個 Deployment。這樣拆是為了讓「應用層」和「模型層」
可以獨立擴縮、獨立更新 —— 模型層要 GPU 且啟動要好幾分鐘，應用層不用。
"""

import os
import re
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PRICE_RE = re.compile(r"[-+]?\d*\.\d+|\d+")


def required(name: str) -> str:
    """必填設定。缺了就開不起來，而不是用一個猜的預設值安靜地跑錯。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing required config: {name}")
    return value


# 全部從環境變數讀，值來自 k8s/base/model.conf 做成的 ConfigMap。
# 這裡不寫任何預設的模型座標 —— 換模型時只該改 ConfigMap。
MODEL_BACKEND = required("MODEL_BACKEND")        # 例：http://model-server:8080
QUESTION = required("PROMPT_QUESTION")
PREFIX = required("PROMPT_PREFIX")

MODEL_NAME = os.getenv("MODEL_NAME", "pricer")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "5"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

app = FastAPI(title="pricer-api")
client = httpx.Client(timeout=REQUEST_TIMEOUT)


class PriceRequest(BaseModel):
    description: str


class PriceResponse(BaseModel):
    price: float
    raw: str
    latency_ms: float


def build_prompt(description: str) -> str:
    """必須與訓練時逐字相同。PREFIX 結尾的 $ 後面沒有空白。"""
    return f"{QUESTION}\n\n{description}\n\n{PREFIX}"


def parse_price(text: str) -> float:
    """後端只回「續寫」部分，不含 prompt，所以直接對回傳文字做 regex。

    不要照抄課程 Modal 版的 result.split("Price is $")[1] ——
    那是因為 HF 的 generate() 會把 prompt 一起回傳，vLLM 和 llama.cpp 不會，
    照抄會 IndexError。
    """
    match = PRICE_RE.search(text.replace("$", "").replace(",", ""))
    return float(match.group()) if match else 0.0


@app.get("/healthz")
def healthz():
    """存活探測：只回報這個行程本身活著，不去戳後端。

    故意不檢查後端 —— 否則後端重啟會害這個 pod 也被 k8s 殺掉，
    把單一故障放大成連鎖故障。
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """就緒探測：後端不通就不該接流量。這裡才需要戳後端。"""
    try:
        resp = client.get(f"{MODEL_BACKEND}/health", timeout=3.0)
        if resp.status_code == 200:
            return {"status": "ready"}
    except httpx.HTTPError:
        pass
    raise HTTPException(status_code=503, detail="model backend not ready")


@app.post("/price", response_model=PriceResponse)
def price(req: PriceRequest):
    payload = {
        "model": MODEL_NAME,
        "prompt": build_prompt(req.description),
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }

    last_error = None
    start = time.perf_counter()
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.post(f"{MODEL_BACKEND}/v1/completions", json=payload)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["text"]
            return PriceResponse(
                price=parse_price(text),
                raw=text,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                # 指數退避。重試是安全的，因為 temperature=0 讓請求具冪等性。
                time.sleep(0.5 * (2 ** attempt))

    raise HTTPException(status_code=503, detail=f"backend failed: {last_error}")
