FROM python:3.12-slim

# 不裝 uv/poetry —— 三個套件而已，多一層工具只是多一個會壞的地方
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 httpx==0.28.1

WORKDIR /app
COPY app.py .

# 以非 root 執行。k8s 端也會設 runAsNonRoot，兩邊都設是為了讓
# 「直接 docker run」和「在 k8s 跑」的行為一致。
RUN useradd --uid 10001 --no-create-home appuser
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
