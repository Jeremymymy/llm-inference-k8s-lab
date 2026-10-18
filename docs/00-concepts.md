# LLM Inference on Kubernetes 實驗室 — 00：核心概念與術語

> 本系列：LLM Inference on Kubernetes Lab
> 這一份的定位：**面試前 30 分鐘複習用**。不含操作步驟，只講原理與術語。
> 建議讀法：第一次先掃過留印象，每做完一個 Phase 回來重讀一次 —— 親手量過之後，這些句子的意思會完全不同。

這份文件裡每個概念後面都會標 **［Phase N 驗證］**，代表你會在那個階段親手把它量出來。**沒有被你量過的概念，面試時不要拿出來講**，因為追問兩層就會露餡。

六個部分：

1. **為什麼 LLM 推論跟一般 web service 不一樣** — prefill / decode 的分裂，這是後面一切的根源
2. **記憶體都去哪了** — 權重、KV cache、PagedAttention
3. **指標** — TTFT / TPOT / throughput / goodput，以及為什麼平均值會騙人
4. **Batching** — continuous batching 為什麼是 vLLM 的核心
5. **量化與 LoRA** — 什麼時候有用、什麼時候沒用
6. **k8s 視角** — LLM 服務跟一般服務的十個差異

---

## Part 1：為什麼 LLM 推論跟一般 web service 不一樣

### 1.1 自迴歸：一次只能生一個 token

一般 web service 是 `request → 計算 → response`，一次算完。

LLM 不是。它一次只能產生**一個 token**，然後把這個 token 接回輸入，再產生下一個。要生 100 個 token，就要跑 100 次前向傳播（forward pass）。

這個「序列相依」的特性無法平行化 —— 第 N 個 token 一定要等第 N-1 個算完。**這是 LLM 推論延遲的物理下限**，不是實作不好。

### 1.2 兩個階段：prefill 與 decode

一次請求被切成兩個性質**完全相反**的階段：

| | **Prefill（預填充）** | **Decode（解碼）** |
|---|---|---|
| 做什麼 | 一次處理完整個 prompt | 一次產生一個 token |
| 平行度 | 高 —— 所有 prompt token 可同時算 | 無 —— 必須一個接一個 |
| 瓶頸 | **算力**（compute-bound） | **記憶體頻寬**（memory-bandwidth-bound） |
| 決定什麼指標 | TTFT | TPOT |
| GPU 利用率 | 容易吃滿 | 很難吃滿 |

**為什麼 decode 是記憶體頻寬瓶頸？** 因為每產生一個 token，都要把**整個模型的權重**從 HBM 讀進計算單元一次，但只做了 batch size 這麼少的乘加。算力大量閒置，卡在搬資料。

這就是為什麼 **batch size 對 decode 階段的吞吐是近乎免費的午餐** —— 權重反正都要讀一次，順便多算幾個序列幾乎不花額外時間。這個洞見直接導出 Part 4 的 continuous batching。

### 1.3 這個 lab 的特殊性：一個極端的 workload

我們的估價模型：

```
輸入：約 150 tokens（固定模板 + 商品描述）
輸出：5 tokens（例如 "129.00"）
```

**輸出只有 5 個 token**，代表這是一個 **prefill 主導**的 workload。含義：

- TTFT 幾乎就等於 E2E latency，TPOT 幾乎不重要
- 它是 **compute-bound** 而非典型的 memory-bound → 量化帶來的加速會比一般 chat 場景小得多［Phase 2 驗證］
- KV cache 需求極小 → 可以開超大並發，也讓單張 GPU 跑多個 replica 變得可行［Phase 3 驗證］

**面試時這點很好用**：大部分人講的 LLM 推論常識（「decode 是瓶頸」「量化能加速」）是以 chat 場景為前提的。你能指出「我的 workload 是 prefill-heavy，所以那些結論不成立」，就證明你是真的量過而不是背的。

---

## Part 2：記憶體都去哪了

GPU 記憶體（HBM）被三件事吃掉：

### 2.1 模型權重

```
權重大小 ≈ 參數量 × 每參數位元組數
```

我們的 Llama-3.2-3B：
- bf16 / fp16（2 bytes）→ 約 **6.4 GB**
- 4-bit 量化（0.5 bytes + 量化常數）→ 約 **2 GB**

這部分是固定的，載入後就不動。

### 2.2 KV cache —— 真正決定你能跑多少並發

每產生一個 token，都要跟**前面所有 token** 做 attention。為了不重算，把每層的 Key 和 Value 存起來，這就是 KV cache。

```
KV cache 大小 = 2（K和V） × 層數 × KV head 數 × head_dim × 序列長度 × batch size × 每元素位元組數
```

關鍵性質：**它隨「序列長度 × 並發數」線性成長**。

**注意是「實際的」序列長度，不是 `--max-model-len`。** 常見的誤解是「max-model-len 設越大，每個請求就預留越多 KV cache」—— 在 vLLM 不成立：它的 KV cache 是按需分配的（見 2.3 PagedAttention），一個 150 token 的請求只佔 150 token 的空間。

`--max-model-len` 真正卡住你的地方是**啟動**：KV cache 至少要放得下一條最長的序列。

| 設定 | 一條最長序列的 KV cache（本 lab 的 3B 模型） | 結果 |
|---|---|---|
| `--max-model-len 131072`（預設） | 約 14 GiB | **在 L4 上很可能起不來**，一條序列就放不下 |
| `--max-model-len 512`（本 lab） | 約 56 MiB | 起得來；但對 150 token 的請求，它跟 32768 佔的 KV cache 一樣多 |

vLLM 啟動時會把「總記憶體 × `--gpu-memory-utilization` − 權重」全部預先劃給 KV cache 當作池子，切成 **blocks**，請求再按需從池子裡拿。啟動 log 會印出容量（`GPU KV cache size: N tokens`），那行是你調校時最該看的數字。［Phase 2 驗證］

### 2.3 PagedAttention —— vLLM 的招牌

**它解決的問題**：傳統做法要為每個序列預先配置「最大長度」的連續記憶體。但你事先不知道會生多長，所以只能按最壞情況配 → 大量記憶體被配了卻沒用到（內部碎片）。

**做法**：借用作業系統虛擬記憶體的分頁概念。把 KV cache 切成固定大小的 block（預設 16 個 token），用一張 block table 把邏輯序列映射到實體 block。序列變長就再配一個 block。

**效果**：碎片率從數十個百分點降到個位數，等於同樣的卡能塞更多並發。額外好處是 block 可以**共享** —— 多個請求的共同前綴指向同一批 block，這就是 prefix caching 的基礎。

> 對本 lab 特別有意義：我們的 prompt 有固定前綴（`What does this cost to the nearest dollar?\n\n`），理論上 prefix caching 可以省掉這段的重複 prefill。列在 `KNOWN-GAPS.md` 的進階題目。

---

## Part 3：指標 —— 以及為什麼平均值會騙人

### 3.1 四個核心指標

| 指標 | 全名 | 定義 | 使用者感受 |
|---|---|---|---|
| **TTFT** | Time To First Token | 送出請求 → 收到第一個 token | 「它有沒有在動」 |
| **TPOT** | Time Per Output Token | 後續每個 token 的間隔（也叫 ITL, Inter-Token Latency） | 「吐字快不快」 |
| **E2E Latency** | End-to-End | 整個請求的總時間 ≈ `TTFT + TPOT × (輸出token數 − 1)` | 「多久拿到完整答案」 |
| **Throughput** | 吞吐 | 每秒完成的請求數，或每秒產出的 token 數 | 營運成本 |

### 3.2 Goodput —— 比 throughput 更誠實的指標

Throughput 有個陷阱：**你永遠可以靠犧牲延遲換到更高的 throughput**。把 batch 開到極大，總 token/s 很好看，但每個使用者都要等 30 秒。

**Goodput** = 每秒完成且**滿足 SLA** 的請求數。例如「TTFT < 500ms 且 TPOT < 50ms」才算數。

面試時能主動提到 goodput，代表你知道 throughput 可以被灌水。

### 3.3 為什麼要看 p95 / p99 而不是平均

LLM 服務的延遲分布是**嚴重右偏**的。原因：

- 請求進來時如果 batch 已滿，會進 waiting queue → 這個請求的延遲是「排隊時間 + 計算時間」
- prompt 長度不一，長 prompt 的 prefill 會拖慢同批次的所有人
- 偶發的 KV cache 不足會觸發 preemption（vLLM 會把某個序列踢出去重算）

平均值會被大量的快請求拉低，完全看不到這些。**p99 才是使用者實際會遇到的最壞情況**，也是 SLA 談的東西。

### 3.4 本 lab 的量測原則

- 每組設定先熱身 1 次（**結果丟掉**，CUDA graph warmup、cache 冷），再正式跑 1 次。這是時間考量下的放寬：單次量測沒有變異數，差距小於約 5% 不下結論
- 固定隨機種子與資料集切片，否則不同組的數字不可比
- 同時記錄 **正確性（MAE）與效能**。只有效能沒有正確性的數字是沒有意義的 —— 你可以讓一個回答完全錯誤的服務跑得飛快。

---

## Part 4：Batching —— continuous batching 為什麼是核心

### 4.1 三代做法

**Static batching（靜態批次）**
湊滿 N 個請求一起送進去，全部算完才一起返回。
問題：一個生 5 token、一個生 500 token 的請求綁在一起，**短的那個要等長的算完才能離開**。GPU 大量空轉。

**Dynamic batching（動態批次）**
等一個小時間窗，把這期間進來的請求湊成一批。
改善了「湊不滿要等」的問題，但**批次一旦開始就鎖定**，上面那個「短的等長的」問題依舊。

**Continuous batching / In-flight batching（連續批次）** ← vLLM 用的
以 **iteration（一次前向傳播）** 為單位排程，而不是以 request 為單位。
每產生一輪 token 後，scheduler 重新決定下一輪的成員：已完成的序列立刻離開並釋放 KV cache，等待中的新請求立刻補進來。

**效果**：GPU 沒有等待空檔，吞吐通常是 static batching 的數倍。

### 4.2 代價：延遲與吞吐的張力

Batch 越大 → 吞吐越高，但每個請求的延遲越長（要跟更多人分享每一輪的時間）。

vLLM 讓你用這幾個旋鈕控制這條曲線：

| 參數 | 控制什麼 | 調大的後果 |
|---|---|---|
| `--max-num-seqs` | 同時在跑的序列數上限 | 吞吐↑，個別延遲↑ |
| `--max-num-batched-tokens` | 一輪最多處理幾個 token | prefill 更積極，但會拖慢正在 decode 的請求 |
| `--gpu-memory-utilization` | 劃給 KV cache 的比例 | 能容納的並發↑，但留給其他東西的空間↓ |

**Chunked prefill**：把長 prompt 的 prefill 切成小塊，跟其他請求的 decode 混在同一輪跑。避免一個長 prompt 的 prefill 把所有正在 decode 的請求卡住（那會讓 p99 很難看）。

---

## Part 5：量化與 LoRA

### 5.1 量化：三個不同的東西別搞混

| 量化對象 | 省什麼 | 影響 |
|---|---|---|
| **權重量化** | 模型佔的 HBM | 最常見。省記憶體，decode 階段也加速（少搬資料） |
| **KV cache 量化** | KV cache 佔的 HBM | 能開更大並發。長 context 場景效益大 |
| **Activation 量化** | 計算精度 | 需要硬體支援（FP8 需 Ada / Hopper 以上），prefill 階段才明顯加速 |

### 5.2 常見格式

| 格式 | 類型 | 特點 |
|---|---|---|
| **NF4**（bitsandbytes） | 4-bit 權重 | QLoRA **訓練**時用的。推論效率不好，vLLM 不走這條 |
| **GPTQ** | 4-bit 權重 | 需要校準資料集，一次性離線量化 |
| **AWQ** | 4-bit 權重 | 保護「重要」通道不量化，通常品質略優於 GPTQ |
| **FP8** | 8-bit 權重+激活 | 需 Ada（L4/L40S）或 Hopper（H100）。品質損失極小 |
| **GGUF Q4_K_M** | 4-bit 權重 | llama.cpp 生態的格式，CPU / Apple Metal 推論用 |

### 5.3 量化什麼時候「不會」加速

這是最常見的誤解。**只壓權重**的量化（weight-only，運算前解回 bf16）省的是**記憶體頻寬**，所以：

- **decode 主導**（長輸出、chat）→ memory-bound → 量化有明顯加速
- **prefill 主導**（本 lab，輸出只有 5 token）→ compute-bound → **weight-only 量化對速度的幫助很小**，主要好處只剩省記憶體

**例外是連激活一起量化的 W8A8**（例如 Ada / Hopper 上的 FP8）：運算本身也用 FP8 tensor core，算力約是 bf16 的兩倍，compute-bound 也會變快。Phase 2 實測：L4 上 vLLM 的 online FP8 走 CUTLASS W8A8 kernel，merged 模型飽和吞吐 53.4 → 73.3 req/s（+37%），MAE 沒有退化。要先看 log 確認選到哪個 kernel，再推論會不會加速。

### 5.4 LoRA：為什麼能熱插拔

LoRA 不動原始權重 `W`，而是學一組低秩的增量：

```
輸出 = W·x + (B·A)·x × (alpha / r)
    其中 A 是 r × d，B 是 d × r，r 遠小於 d
```

因為 `W` 沒被改，**一個 base model 可以同時掛多個 adapter**，每個請求指定要用哪一個。這直接對應 on-prem 最常見的省 GPU 架構：多租戶共用一個 base，各自帶自己的 adapter。［Phase 2 驗證］

**我們這個 adapter 的參數**（來源：`adapter_config.json`，不要憑記憶）：

```
r = 256, alpha = 512, dropout = 0.1
target_modules = q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj（七個全含）
```

`r=256` 是相當大的 rank（常見是 8～64）。兩個後果：

1. **vLLM 的 `--max-lora-rank` 預設是 16**。不設成 256 的話，vLLM **啟動時就會拒絕載入** adapter。［Phase 1 會刻意重現這個失敗，並跟真正無聲的失敗對照］
2. **runtime LoRA 的 kernel 開銷跟 rank 成正比**。rank 越大，「不 merge 直接跑」的代價越高，把權重 merge 進 base 再 serve 可能反而更快。

### 5.5 Runtime LoRA vs Merged 權重

| | **Runtime LoRA** | **Merge 進 base** |
|---|---|---|
| 記憶體 | 一份 base + N 個小 adapter | 每個 adapter 一份完整模型 |
| 速度 | 每層多一次低秩矩陣運算（r 越大越貴） | 跟原生模型完全一樣 |
| 彈性 | 可熱插拔、可多租戶 | 換模型要重載整個服務 |
| 適用 | 多個 adapter、流量分散 | 單一 adapter、追求極致延遲 |

［Phase 2 會把兩邊的 throughput 量出來，這是本 lab 的原創實驗］

---

## Part 6：k8s 視角 —— LLM 服務跟一般服務的十個差異

這一節是整份 lab 的骨幹。每一條都對應一個具體的 k8s 設定。

| # | 差異 | k8s 上的後果 | 在哪裡處理 |
|---|---|---|---|
| 1 | **啟動要 1–3 分鐘**（載權重 + CUDA graph warmup） | 用 `livenessProbe` 顧啟動會無限重啟；必須用 `startupProbe` 吸收 | Phase 0 |
| 2 | **鏡像很大**（vLLM image 10GB+） | `imagePullPolicy` 與 pre-pull 策略變重要；on-prem 沒有 ECR | Phase 3 |
| 3 | **模型權重不該進鏡像** | 需要 PVC / initContainer / 內部物件儲存 | Phase 0、0.5 |
| 4 | **請求可能跑很久**（streaming） | `terminationGracePeriodSeconds` 要 > 最長生成時間；Ingress 的 `proxy_buffering` 要關 | Phase 0 |
| 5 | **GPU 不可切分也不可超賣** | `nvidia.com/gpu` 只能是整數；單卡無法零停機滾動更新 | Phase 3 |
| 6 | **CPU 使用率不代表忙碌** | HPA 不能看 CPU，要看排隊長度等自訂指標 | Phase 0 |
| 7 | **擴容延遲遠大於取樣週期**（新 pod 3 分鐘才 ready） | HPA 的 `behavior.scaleUp.stabilizationWindowSeconds` 不調會震盪 | Phase 0 |
| 8 | **記憶體用量是預先劃定的**（vLLM 啟動就吃滿 90%） | limit 難設；OOMKilled 的表現跟一般服務不同 | Phase 0 |
| 9 | **需要 `/dev/shm`**（多進程共享記憶體） | k8s 預設只給 64MB，要掛 `emptyDir: {medium: Memory}` | Phase 3 |
| 10 | **GPU 利用率 100% 是假的** | 需要 DCGM 的 `SM_ACTIVE` 才知道算力有沒有被浪費 | Phase 3 |

---

## 術語速查

| 術語 | 一句話 |
|---|---|
| **Prefill** | 處理輸入 prompt 的階段，可平行，compute-bound |
| **Decode** | 逐個產生輸出 token 的階段，不可平行，memory-bandwidth-bound |
| **TTFT** | 從送出到收到第一個 token 的時間 |
| **TPOT / ITL** | 之後每個 token 的間隔 |
| **Goodput** | 每秒完成且滿足 SLA 的請求數 |
| **KV cache** | 存下每層的 Key/Value 避免重算，大小隨「序列長度 × 並發」成長 |
| **PagedAttention** | 把 KV cache 分頁管理，降低碎片、支援共享 |
| **Continuous batching** | 以 iteration 為單位動態換進換出批次成員 |
| **Chunked prefill** | 把長 prompt 的 prefill 切塊，跟 decode 混合排程 |
| **Preemption** | KV cache 不足時，vLLM 把某序列踢出重算（不是 k8s 的 preemption） |
| **LoRA** | 低秩增量微調，不動原權重，可熱插拔 |
| **Time-slicing** | 讓一張 GPU 對 k8s 謊報成 N 張，無記憶體與故障隔離 |
| **MIG** | 硬體層切分 GPU，有真正的隔離；僅 A100/H100 等支援 |
| **DCGM** | NVIDIA 的 GPU 監控代理，提供比 `nvidia-smi` 細的指標 |

---

## 下一站

→ [`01-setup.md`](01-setup.md)：前置作業。先把 AWS 的 GPU 配額申請送出去（那是唯一會卡住整個計畫的事），再回頭做本機的部分。
