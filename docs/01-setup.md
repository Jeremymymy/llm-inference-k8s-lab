# LLM Inference on Kubernetes 實驗室 — 01：前置作業

> 這一份的定位：**開工前的準備**。
> 讀法：**Part 1 今天就做完**，它是唯一有外部等待時間的事。其他部分可以慢慢來。

---

## Part 0：這份 lab 在做什麼（先對齊心智模型）

你要建一個「商品估價」的推論服務，並把它一路從本機推到 GPU 上的 Kubernetes。

```
商品描述 ──► 推論服務 ──► 估價（美元）
             ├─ Llama-3.2-3B (base)
             └─ + LoRA adapter (估價任務微調)
```

**為什麼用這個 workload 而不是隨便找個模型 demo：**

| 一般教學 demo | 這個 lab |
|---|---|
| 「跑起來會回答」就算成功 | 有**客觀正確性門檻**（MAE ≈ $39.85） |
| 沒辦法判斷調校有沒有搞壞東西 | 每次調校都同時量正確性與效能 |
| 面試只能說「我部署過」 | 可以說「我量到 X，因為 Y，所以我選 Z」 |

**驗收門檻的來源**：Ed Donner 課程 `week7/results.ipynb` 的 leaderboard，同一個模型、同一份測試集的公開數字。

| 方法 | MAE |
|---|---|
| 固定猜平均值 | $106.18 |
| 人類（課程作者） | $87.62 |
| XGBoost | $68.23 |
| 未微調的 Llama 3.2 (4-bit) | $110.72 |
| **微調後（本 lab 要復現的）** | **$39.85** |

所以：**如果你的服務跑出 $110 左右，代表 LoRA 根本沒生效**（這正是 Phase 1 那個 `--max-lora-rank` 陷阱的症狀）。有了這個門檻，你不用猜，數字會直接告訴你。

---

## Part 1：今天就送出 AWS GPU 配額申請 ⏰

**目的**：這是整個計畫裡唯一「你做不了、只能等」的事。先送出，後面所有本機工作都可以平行進行。

### 1.1 做法

1. 登入 AWS Console，切到你打算用的 region（建議挑一個 GPU 供給穩定的，例如 `us-east-1` 或 `us-west-2`）
2. 進 **Service Quotas → AWS services → Amazon Elastic Compute Cloud (Amazon EC2)**
3. 搜尋這兩個配額，**兩個都要申請**：

| 配額名稱 | 代碼 | 用途 |
|---|---|---|
| Running On-Demand G and VT instances | `L-DB2E81BA` | 按需執行個體 |
| All G and VT Spot Instance Requests | （搜尋 "G and VT Spot"） | Spot（便宜 60–70%） |

4. 兩個都 **Request increase at account level**，值填 **8**（單位是 vCPU）

直達連結（把 region 換成你要的）：
```
https://us-east-1.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/L-DB2E81BA
```

### 1.2 為什麼這樣做

- **預設值是 0**。新帳號完全不能開任何 GPU 機器，不是「額度不夠」而是「一台都不行」。
- **為什麼填 8 而不是 64**：保守的申請通常當天就過，誇張的申請會被轉人工審查拖好幾天。`g6.xlarge` 是 4 vCPU，8 就夠你同時開兩台或留一台備援。不夠之後再加。
- **為什麼 Spot 要另外申請**：它是獨立的配額項目。你會想用 Spot 省錢，但很多人是到要開機那天才發現沒申請。
- **單位是 vCPU 不是機器台數**。這是最常被誤解的地方。

### 1.3 驗證

```bash
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA \
  --region us-east-1 --query 'Quota.Value'
```
回傳 `0.0` 代表還沒過，回傳 `8.0` 代表已生效。

### ⚠️ 陷阱

- **配額是 per-region 的**。在 `us-east-1` 申請通過，不代表 `ap-northeast-1` 能開。決定好 region 再申請，之後不要換。
- 核准通知信有時候會晚於實際生效，直接用上面的指令查比較準。

---

## Part 2：設定預算警示（同樣是今天做）

**目的**：這個 lab 全程預算約 $60。忘記關機一週的 g6.xlarge 是 $135。先裝防呆。

### 2.1 做法

Console → **Billing and Cost Management → Budgets → Create budget**

建兩個：

| 名稱 | 類型 | 金額 | 警示 |
|---|---|---|---|
| `lab-monthly-hard` | Cost budget（月） | $80 | 實際 50% / 80% / 100% 都寄信 |
| `lab-daily-canary` | Cost budget（日） | $5 | 實際 100% 寄信 |

**兩個都不要加 filter** —— 範圍保持預設的「全部服務、全部 region」，建立精靈裡的 Filters 一律不要動。

### 2.2 為什麼這樣做

月預算抓不到「忘記關機」—— 等你收到 80% 警示時，錢已經花掉了。**日預算才是真正的煙霧偵測器**：正常使用一天不會超過 $5，一收到日警示就代表有東西忘了關。

**不加 filter 的理由**：偵測器不該預先假設火從哪裡燒起來。如果把範圍限縮成 `Service = EC2`，那 EKS 控制平面（$73/月）、S3、CloudWatch logs、跨區流量全部逃過警示 —— 而這些正是你「不知道自己開了」的東西。會咬人的一定是你沒預期到的那一項。

唯一的例外：這個 AWS 帳號同時有 lab 以外的支出，$5/日 的基線就不成立。解法是改用 lab 專用帳號，而不是加 filter 讓警示變半盲。

### 2.3 驗證

`Budgets` 頁面看到兩筆，狀態 OK。不需要等它觸發。

### ⚠️ 陷阱

- AWS 的帳務資料有數小時延遲，預算警示**不是即時的**。它是安全網，不是關機開關。真正的防護是 `91-cost-and-teardown.md` 的離桌檢查清單。
- 用 root 帳號以外的 IAM user 時，要先在 Billing preferences 開啟 IAM 存取才看得到帳單頁面。

---

## Part 3：HuggingFace 授權與 token

**目的**：base model 是 gated repo，沒接受授權的話後面每一步都會 401。

### 3.1 做法

1. 到 https://huggingface.co/meta-llama/Llama-3.2-3B 點 **Agree and access repository**，等核准（通常幾分鐘到幾小時）
2. 到 https://huggingface.co/settings/tokens 建一個 **Read** 權限的 token
3. 存到本機（這個目錄已經 gitignore）：

```bash
cd ~/Desktop/side-projects/llm-inference-k8s-lab
printf '%s' 'hf_你的token' > private/hf-token
chmod 600 private/hf-token
```

### 3.2 為什麼這樣做

- **為什麼是檔案不是環境變數檔**：後面每個 k8s 階段都要把它做成 Secret，從檔案建最直接：
  ```bash
  kubectl create secret generic hf-token --from-file=token=private/hf-token
  ```
  少一次手動複製貼上，就少一次貼錯或貼進 git 的機會。
- **為什麼只給 Read 權限**：這個 lab 只需要下載。之後你自己 fine-tune 要上傳時再另外開一個 Write token。

### 3.3 驗證

```bash
curl -s -H "Authorization: Bearer $(cat private/hf-token)" \
  https://huggingface.co/api/models/meta-llama/Llama-3.2-3B \
  | python3 -c "import json,sys; print('OK:', json.load(sys.stdin)['modelId'])"
```
印出 `OK: meta-llama/Llama-3.2-3B` 就通了。出現 `401` 或 `GatedRepo` 代表授權還沒下來。

### ⚠️ 陷阱

- **base 不是 Instruct**。`meta-llama/Llama-3.2-3B` 和 `meta-llama/Llama-3.2-3B-Instruct` 是兩個不同的 repo，要分別接受授權。我們要的是**沒有 -Instruct 的那個**，因為 adapter 是在 base 上訓練的。
- Llama 的授權對「歐盟地區的多模態模型」有額外限制，純文字模型不受影響，但條款要自己看過。

---

## Part 4：本機工具檢查

### 4.1 做法

```bash
for t in kubectl helm k9s docker orb kind uv git; do
  printf '%-10s %s\n' "$t" "$(command -v $t || echo '(缺)')"
done
```

缺的補上（macOS）：
```bash
brew install kind          # Phase 0.5 才會用到
brew install kubectl helm k9s
```

`orb`（OrbStack）從 https://orbstack.dev 裝。裝完在 OrbStack 設定裡把 **Kubernetes** 打開。

### 4.2 為什麼需要這些

| 工具 | 用在哪 | 為什麼是它 |
|---|---|---|
| **OrbStack** | Phase 0 單節點 k8s | 比 Docker Desktop 省資源；LoadBalancer 與 `*.k8s.orb.local` 萬用字元 DNS 開箱即用，不用裝 MetalLB |
| **kind** | Phase 0.5 多節點 | 唯一能在本機開多節點的輕量方案，且能模擬 on-prem 的網路（配 MetalLB） |
| **helm** | Phase 0、3 | kube-prometheus-stack、ingress-nginx、GPU Operator 都只有 Helm 安裝路徑 |
| **k9s** | 全程 | 比反覆打 `kubectl get pods -w` 快很多，debug 時差別明顯 |
| **uv** | 全程 | 跑 `eval/measure.py` 不用管 venv：`uv run --with datasets ...` |

### ⚠️ 陷阱 —— 你的機器規格決定了做法

你的機器是 **MacBook Air M4 / 16GB / 被動散熱**。三個硬性後果，貫穿整份文件：

1. **本機不產生任何效能數字。** Air 沒有風扇，持續壓測會降頻，量到的數字沒有意義也無法重現。本機只用來驗證**正確性**與 **k8s 行為**。所有效能數字都來自 Phase 1 之後的 GPU 機器。
2. **不要同時開 OrbStack k8s 和 kind。** kind 每個 node 是一個容器，三節點空載約 1.5–2GB，加上 OrbStack 的 VM 和模型會把 16GB 吃爆。切換時先 `orb stop k8s`。
3. **磁碟要留 30GB 以上。** 模型轉檔過程會有暫存檔。開工前 `df -h /` 確認。

---

## Part 5：建立 repo 與模型設定

### 5.1 做法

repo 已經建好了，結構是：

```
llm-inference-k8s-lab/
├── k8s/base/model.conf      ← 模型座標的唯一真實來源
├── eval/measure.py        ← 評估工具
├── private/               ← 憑證（gitignore）
└── docs/                  ← 你正在讀的東西
```

把設定載進 shell：
```bash
cd ~/Desktop/side-projects/llm-inference-k8s-lab
while IFS='=' read -r k v; do export "$k=$v"; done < <(grep -E '^[A-Z_]+=' k8s/base/model.conf)
echo "$BASE_MODEL / $ADAPTER_REPO / rank=$LORA_RANK"
```

### 5.2 為什麼所有座標集中在一個檔

你之後會自己跑一次課程 week7 的 fine-tune，然後把成果換進來。到那時候，**你只需要改 `k8s/base/model.conf` 的兩行**：

```diff
-ADAPTER_REPO=ed-donner/price-2025-11-28_18.47.07
-ADAPTER_REVISION=b19c8bfea3b6ff62237fbb0a8da9779fc12cefbd
+ADAPTER_REPO=你的帳號/price-你的RUN_NAME
+ADAPTER_REVISION=你的commit
```

其餘所有文件、腳本、manifest 都從這裡取值（k8s 端用 `kubectl create configmap model-config --from-env-file=k8s/base/model.conf`）。如果模型座標散落在十幾個 YAML 裡，換模型那天你會漏掉其中兩個，然後花一個下午 debug 為什麼 MAE 不對。

**這也是專案敘事的核心**：不是「我部署了別人的模型」，而是「我學會 fine-tune，然後自己把它部署上線」。

### 5.3 這個 adapter 的關鍵事實

從 `adapter_config.json` 實際抓下來的（不是憑記憶）：

```
base:   meta-llama/Llama-3.2-3B
r:      256          ← 很大，常見是 8~64
alpha:  512
dropout: 0.1
target_modules: q_proj k_proj v_proj o_proj gate_proj up_proj down_proj（七個全含）
```

**`r=256` 會在 Phase 1 咬你一口**，因為 vLLM 的 `--max-lora-rank` 預設是 16。先記住這件事。

---

## Part 6：評估工具怎麼用

### 6.1 做法

`eval/measure.py` 可以打**任何** OpenAI 相容的 `/v1/completions` 端點：

```bash
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 \
  --model pricer \
  --size 200 \
  --label "說明這次跑的是什麼"
```

輸出會給你 MAE / R² / hit rate / p50-p95-p99 延遲，最後印一行可以直接貼進 `RESULTS.md` 的表格列。

### 6.2 為什麼是這些指標、為什麼是 200 筆

- **MAE 是主指標**，因為 leaderboard 用的是它。換指標就失去對照基準。
- **200 筆**是課程的慣例。筆數改了，數字就不能跟 $39.85 比 —— 樣本數不同，誤差的信賴區間也不同。
- **hit rate**（誤差 < $40 或 < 20%）是額外加的。MAE 會被少數離群值主導，hit rate 能看出「大部分時候準不準」。兩個一起看比較不會被騙。
- **延遲百分位數**在這裡只是順便量的粗略值。真正的效能量測在 Phase 2 用 `vllm bench serve`。

### 6.3 驗證

現在還沒有任何服務可以打，所以先確認它至少能載資料集：

```bash
uv run --with datasets python -c "
from datasets import load_dataset
d = load_dataset('ed-donner/items_prompts_full', split='test')
print('筆數:', len(d))
print('--- 第一筆 prompt ---')
print(repr(d[0]['prompt'][:200]))
print('--- 正確答案 ---', d[0]['completion'])
"
```

你應該看到 prompt 長這樣（注意結尾）：
```
'What does this cost to the nearest dollar?\n\nTitle: ...\n\nPrice is $'
```

### ⚠️ 陷阱 —— prompt 必須逐字相同

**`Price is $` 的 `$` 後面沒有空白。** 差一個字元，模型看到的就不是它訓練時的分布，MAE 會莫名其妙變差而你找不到原因。

好消息是：資料集的 `prompt` 欄位**已經包含完整模板**，所以 `measure.py` 直接把它送出去，不自己組。你唯一要組 prompt 的時機是 Phase 0 之後寫 `pricer-api` 那個服務時 —— 那時候回來抄 `k8s/base/model.conf` 裡的 `PROMPT_QUESTION` / `PROMPT_PREFIX`。

另一個要記住的變因：課程在**訓練**時把商品描述截斷到 110 tokens，但 week8 的線上服務餵的是**未截斷**的描述。這是既有實作的不一致。本 lab 直接用資料集的 prompt（已截斷），所以跟訓練分布一致；但如果你之後接自己的真實輸入，記得這個差異。

---

## 開工前的檢查清單

- [x] AWS G/VT 的 **On-Demand** 配額申請已送出
- [x] AWS G/VT 的 **Spot** 配額申請已送出
- [x] 月預算 $80 + 日預算 $5 都建好
- [x] HuggingFace 已接受 `meta-llama/Llama-3.2-3B` 授權，token 存在 `private/hf-token`
- [x] `kubectl` / `helm` / `k9s` / `kind` / `uv` 都有
- [x] OrbStack 的 Kubernetes 已開啟
- [x] `df -h /` 顯示剩餘空間 > 30GB
- [x] `eval/measure.py` 能成功載入資料集

配額還在等的時候，直接往下走 —— Phase 0 和 Phase 0.5 完全不需要 AWS。

---

## 下一站

→ [`02-phase0-local-k8s.md`](02-phase0-local-k8s.md)：在本機把模型跑起來拿到第一個 MAE，然後建立完整的 k8s 骨架與可觀測性。全程零成本。
