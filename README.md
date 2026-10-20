# LLM Inference on Kubernetes — 一個可維運的估價服務

把一個 QLoRA 微調過的商品估價模型，從「在筆記本裡跑得起來」做到「在 Kubernetes 上可維運」。
本機沒有 NVIDIA GPU，所以 k8s 的部分全部零成本在本機完成，只有真正需要 GPU 的部分才租雲端。

> **這是一個學習實驗室**，不是生產系統。已知的邊界誠實列在 [`KNOWN-GAPS.md`](KNOWN-GAPS.md)。

---

## 這個專案在解什麼問題

課程教到「用 Modal 把微調後的模型跑起來」就結束了。但那距離一個能維運的服務還有一段：

| 課程的版本 | 這個 lab 的版本 |
|---|---|
| `modal.Cls.from_name(...)` 一行呼叫 | k8s Deployment + Service + Ingress |
| 沒有 timeout、沒有 retry | 逾時、指數退避重試、優雅關機 |
| 沒有健康檢查 | startup / readiness / liveness 三種 probe 各司其職 |
| 不知道它有沒有在飽和 | Prometheus + Grafana + 依佇列長度擴縮的 HPA |
| 「跑起來就好」 | **每次調校都同時量正確性（MAE）與效能** |

**最後一列是這個 lab 的核心。** 因為這個任務有公開的 baseline（MAE $39.85），所以每個設定變更都有客觀對錯。實測：請求的 model 名稱打到 base 時，服務完全正常、probe 全綠、API 回傳合法的價格，但 MAE 從 $40.74 變成 $200.29 —— 任何 health check 都抓不到。

---

## 亮點（全部是實測數字，g6.xlarge / NVIDIA L4）

- **k8s 這層沒有改變模型行為**：同一個模型搬進 k3s，MAE $40.79，Docker 版 $40.74（差 0.1%，屬批次雜訊）
- **吞吐翻倍、品質不降**：LoRA 合併進權重 35.77 → 53.38 req/s（+49%），再做 FP8 量化 → 73.28 req/s（+37%）；MAE $40.80 → $40.15
- **把 GPU time-slicing 的代價拆開**：2 pod 合計吞吐 −19%；加一組對照實驗拆出「顯存切小、KV 變少」佔 10.7%、「兩個 pod 輪流用 GPU」佔 8.8%
- **「GPU 使用率 100%」只說對一半**：DCGM 量到 SM 忙碌 95%，但 Tensor Core 只有 52% 的時間在算
- **單卡滾動更新的真實停機 191 秒**：量化「要不要多買一張卡」的決策依據
- **GPU 總花費約 $6**：k8s 的部分全在本機零成本完成，只有需要 GPU 的實驗才租雲端

---

## 架構

```
                    ┌──────────────────────────────────────────┐
   HTTP             │  Kubernetes                              │
  ────────►  Ingress├─► pricer-api ──Service DNS──► model-server│
                    │   (FastAPI)                   (vLLM/GPU)  │
                    │   · 組 prompt                  · LoRA     │
                    │   · timeout + retry            · KV cache │
                    │   · 解析價格                    · batching │
                    │                                    │      │
                    │                               /metrics    │
                    │                                    ▼      │
                    │      Prometheus ──► prometheus-adapter    │
                    │           │                        │      │
                    │        Grafana              HPA ◄──┘      │
                    │                        (依佇列長度擴縮)     │
                    └──────────────────────────────────────────┘
```

**同一份 Kustomize base 跑在三種環境**，只換 overlay：

| 環境 | 模型後端 | 用途 |
|---|---|---|
| OrbStack 單節點（Mac） | llama.cpp / CPU | k8s 骨架、可觀測性、故障演練 |
| kind 三節點（Mac） | llama.cpp / CPU | 排程、drain/PDB、資源治理、MetalLB |
| k3s + L4 GPU（EC2） | **vLLM / CUDA** | 真實效能、GPU 排程、time-slicing |

---

## 成果

> 完整紀錄（含量測條件、原始數字與結論）在 [`RESULTS.md`](RESULTS.md)。

### 正確性（MAE，越低越好）

| 設定 | MAE | 備註 |
|---|---|---|
| 課程 leaderboard 基準 | **$39.85** | 驗收門檻 |
| 未微調的 base model（leaderboard） | $110.72 | 課程 evaluator，跟下面不可直接比 |
| 未微調的 base model（本 repo eval，vLLM bf16） | $200.29 | 「LoRA 沒生效」的指紋 |
| llama.cpp Q8_0 + LoRA（Mac） | $40.79 | Phase 0 |
| vLLM bf16 + LoRA rank 256 | $40.74 | Phase 1 主線 |
| 同上，跑在 k3s 裡 | $40.79 | Phase 3；與主線一致 → k8s 這層沒改變行為 |
| vLLM bf16 + merged 權重 | $40.80 | 與主線一致 |
| vLLM FP8 + merged 權重 | $40.15 | 沒有可量到的量化代價 |

### 效能（g6.xlarge / L4 24GB，vLLM 0.31.0，飽和、單次量測）

| 設定 | 吞吐 | p95 TTFT | KV cache size |
|---|---|---|---|
| `--max-model-len 131072`（預設） | 起不來 | — | — |
| `--max-model-len 32768`（runtime LoRA） | 35.92 req/s | 13,282 ms | 84,880 |
| `--max-model-len 512`（runtime LoRA） | 35.77 req/s | 13,341 ms | 84,864 |
| merged bf16 | 53.38 req/s | 8,966 ms | 106,400 |
| FP8 量化（merged） | 73.28 req/s | 6,512 ms | 127,520 |
| k3s 1 pod（0.85，runtime LoRA） | 34.54 req/s | p99 14,198 ms | 93,952 |
| time-slicing 2 replica（各 0.42） | 28.13 req/s（−19%） | p99 17,442 ms | 5,232 / pod |

### 成本

| 階段 | 花費 |
|---|---|
| Phase 0 + 0.5（本機） | $0 |
| Phase 1–2（GPU，約 3.5 小時） | 約 $3（依 log 時間戳估算） |
| Phase 3（GPU，約 3.5 小時） | 約 $2.8（不含 EBS） |

---

## 做了什麼

- **Phase 0** — 本機 k8s 骨架與可觀測性：probe 分工、Prometheus + prometheus-adapter + 依佇列長度的 HPA（$0）
- **Phase 0.5** — kind 三節點：排程、drain/PDB、ResourceQuota、PriorityClass 搶佔（$0）
- **Phase 1** — 雲端 GPU + vLLM 服務化與正確性驗證（2026-10-18）
- **Phase 2** — 效能調校、壓測、量化對照（2026-10-18）
- **Phase 3** — k3s + GPU Operator + time-slicing + DCGM（2026-10-20；HPA 接 vLLM 指標未在 GPU 上實跑，見 [`KNOWN-GAPS.md`](KNOWN-GAPS.md)）
- **Phase 4** — EKS：未實跑，只做了雲端↔地端對照表

---

## 完整過程

每個 Phase 的逐步紀錄、排錯速查、事故演練、EKS 對照表，都在 [**detail 分支**](https://github.com/Jeremymymy/llm-inference-k8s-lab/tree/detail)。

---

## 目錄結構

```
.
├── k8s/
│   ├── base/                  兩種環境共用，不含任何 GPU 設定
│   │   └── model.conf         ← 模型座標的唯一真實來源
│   ├── overlays/local/        llama.cpp / CPU
│   ├── overlays/gpu/          vLLM / CUDA + time-slicing
│   ├── governance/            PriorityClass、Quota、PDB、batch Job
│   ├── observability/         ServiceMonitor、HPA、prometheus-adapter
│   └── kind/                  三節點叢集設定
├── src/pricer-api/            對外門面（FastAPI）
└── eval/measure.py            評估工具：MAE / R² / hit rate / 延遲百分位
```

**換成自己微調的模型**：只改 `k8s/base/model.conf` 的 `ADAPTER_REPO` 與 `ADAPTER_REVISION`。其他檔案一行都不用動。

---

## Attribution

**模型、資料集與評估基準都來自 [Ed Donner 的 LLM Engineering 課程](https://github.com/ed-donner/llm_engineering)**：

| 項目 | 來源 |
|---|---|
| Base model | `meta-llama/Llama-3.2-3B`（Meta，需接受 Llama 3.2 授權） |
| LoRA adapter | `ed-donner/price-2025-11-28_18.47.07` |
| 資料集 | `ed-donner/items_prompts_full`（源自 `McAuley-Lab/Amazon-Reviews-2023`） |
| MAE baseline | 該課程 `week7/results.ipynb` 的 leaderboard |

**微調不是我做的，部署與維運才是這個 repo 的內容。**
（規劃中：自己跑一次課程的 week7 微調，再把 adapter 換成自己的。）

---

## English summary

Taking a QLoRA fine-tuned product-pricing model from "runs in a notebook" to "operable on Kubernetes."

No local NVIDIA GPU, so the work splits in two: **all Kubernetes work is done locally at zero cost** (single-node OrbStack for the service skeleton and observability; a three-node kind cluster for scheduling, drain/PDB, and resource governance), and **only the parts that genuinely need a GPU** run on a rented `g6.xlarge` (NVIDIA L4) — vLLM serving, performance tuning, and finally k3s to bring both halves together.

The organising principle: **every experiment measures correctness (MAE) alongside performance.** The task has a published baseline of $39.85 MAE, so every configuration change has an objective pass/fail. Measured: when requests hit the base model instead of the adapter, the service stays fully healthy — probes green, API returning well-formed prices — yet MAE jumps from $40.74 to $200.29, which no health check can detect.

Deliberately **not** EKS-first: GPU-on-Kubernetes fundamentals are fully learnable on single-node k3s, which is also closer to the on-prem target environment, and it avoids the $73/month control-plane rent. A cloud↔on-prem mapping table is on the [detail branch](https://github.com/Jeremymymy/llm-inference-k8s-lab/blob/detail/docs/07-phase4-eks.md).

Model, dataset and baseline come from [Ed Donner's LLM Engineering course](https://github.com/ed-donner/llm_engineering) — see Attribution above. The deployment and operations work is mine.
