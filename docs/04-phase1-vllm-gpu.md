# LLM Inference on Kubernetes 實驗室 — 04：Phase 1，雲端 GPU 上的 vLLM

> 成本：**約 $16**（g6.xlarge 跑 20 小時）。這是第一次花錢的階段。
> 產出：vLLM 服務化、正確性對齊 MAE $39.85、以及兩個原創實驗的數據。
> **刻意不上 k8s。** 理由見下。

---

## 這一階段的目的

**先單獨搞懂 inference 這一層。**

GPU 問題的標準排查順序是由下往上：

```
nvidia-smi 看得到卡嗎
   └─► docker run --gpus all 容器裡看得到嗎
          └─► device plugin 讓 k8s 看得到嗎
                 └─► pod 真的拿到卡了嗎
```

一開始就疊上 k8s，出問題時你會分不清是哪一層。**Phase 1 不是「退回」，是先確認 runtime 正確再往上包一層** —— 而且 Phase 1 和 Phase 3 用的是同一台機器，這一步實質上就是 Phase 3 的前置除錯。

**七個部分：**

1. 挑機型與開機
2. 磁碟與模型快取
3. 把 vLLM 跑起來
4. 正確性驗證 —— 對齊 MAE $39.85
5. 刻意失敗：`--max-lora-rank` 的無聲截斷
6. 原創實驗：runtime LoRA vs merged 權重
7. 四方對照表

---

## ⚠️ 開始前：先讀成本紀律

**這一階段每小時燒 $0.80。** 在做任何事之前，先把 [`91-cost-and-teardown.md`](91-cost-and-teardown.md) 讀過一遍，特別是「離桌檢查清單」。

三條鐵律：
1. **離開電腦前一定 `aws ec2 stop-instances`**（stop 不是 terminate，EBS 留著下次用）
2. **每天結束對一次帳**：`aws ce get-cost-and-usage`（指令在成本那份文件裡）
3. **不確定有沒有東西在跑就查**：`aws ec2 describe-instances --filters "Name=instance-state-name,Values=running"`

---

## Part 1：挑機型與開機

### 1.1 做法

先確認配額下來了：

```bash
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA \
  --region us-east-1 --query 'Quota.Value'
```

找一個帶 NVIDIA 驅動的 AMI（省得自己裝驅動）：

```bash
aws ssm get-parameters --region us-east-1 \
  --names /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameters[0].Value' --output text
```

開機（把 `<AMI>` / `<KEYPAIR>` / `<SG>` 換成你的）：

```bash
aws ec2 run-instances --region us-east-1 \
  --image-id <AMI> \
  --instance-type g6.xlarge \
  --key-name <KEYPAIR> \
  --security-group-ids <SG> \
  --block-device-mappings '[{
    "DeviceName":"/dev/sda1",
    "Ebs":{"VolumeSize":150,"VolumeType":"gp3","DeleteOnTermination":true}
  }]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=pricer-lab},{Key=Project,Value=llm-inference-lab}]'
```

### 1.2 為什麼是 g6.xlarge

| 機型 | GPU | 記憶體 | 約略單價 | 判斷 |
|---|---|---|---|---|
| **g6.xlarge** | L4 (Ada) | 24GB | **$0.8048/hr** | ✅ **推薦**。支援 bf16 與 FP8，量化實驗做得全 |
| g5.xlarge | A10G (Ampere) | 24GB | 約 $1.0/hr | 可以，但比較貴且沒有 FP8 |
| g4dn.xlarge | T4 (Turing) | 16GB | 約 $0.53/hr | ⚠️ **沒有 bf16**，要加 `--dtype half`；部分量化 kernel 不支援。省下的錢會被多花的時間吃掉 |

我們的模型只有 3B（bf16 約 6.4GB），其實 16GB 就夠。**選 g6 不是為了跑得動，是為了 Phase 2 的量化實驗做得完整** —— L4 是 Ada 架構，有 FP8 硬體支援，這讓「bf16 / FP8 / AWQ / GPTQ」的對照表能真的做出四組數據。

**150GB 的 root EBS**：模型快取（bf16 權重 6.4GB + 各種量化版本）加上 vLLM 的 image（10GB+），100GB 會很緊。gp3 每月約 $12，但你只會用幾天。

### 1.3 安全群組

```bash
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --region us-east-1 \
  --group-id <SG> --protocol tcp --port 22 --cidr "${MY_IP}/32"
```

**只開 22，只開給你自己的 IP。** vLLM 的 port 走 SSH 通道，不要對外開放：

```bash
ssh -i <key> -L 8000:localhost:8000 ubuntu@<公有IP>
```

### ⚠️ 陷阱

- **`stop` 不是 `terminate`。** stop 之後 EBS 還在（每月約 $12），下次 start 模型快取還在，不用重下載。terminate 則是全部消失。
- **stop/start 後公有 IP 會變**（除非綁 Elastic IP，但閒置的 EIP 也要收費）。每次重連前先查新 IP。
- **spot 便宜 60–70% 但會被中斷。** 學習階段可以用，但要接受「跑到一半機器不見了」。模型快取在 EBS 上所以損失有限。
- 開機後**第一件事**就是設好關機提醒。真的。

---

## Part 2：磁碟與模型快取

### 2.1 做法

```bash
# SSH 進去之後
nvidia-smi                      # 確認看得到 L4
df -h /                         # 確認磁碟空間

# 把 HF cache 指到有空間的地方
echo 'export HF_HOME=/opt/dlami/nvme/hf' >> ~/.bashrc 2>/dev/null || \
echo 'export HF_HOME=$HOME/hf-cache' >> ~/.bashrc
source ~/.bashrc
mkdir -p "$HF_HOME"

# 憑證
mkdir -p ~/private && chmod 700 ~/private
# 從本機複製上來：
#   scp -i <key> private/hf-token ubuntu@<IP>:~/private/hf-token
export HF_TOKEN=$(cat ~/private/hf-token)
```

### 2.2 為什麼 HF cache 的位置很重要

模型下載一次要幾分鐘。如果 cache 放在**會隨容器消失**的地方，你每次 `docker run` 都要重下載一次 —— 在按小時計費的機器上，這是純粹的浪費。

把 `HF_HOME` 指到 EBS 上的固定路徑，然後每次 `docker run` 都掛載它：

```bash
-v "$HF_HOME:/root/.cache/huggingface"
```

**這個習慣在 Phase 3 會變成 PVC 或 initContainer**，是同一個問題的不同層次解法。

### 2.3 驗證

```bash
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
# NVIDIA L4, 23034 MiB, 8.9
```

`compute_cap 8.9` = Ada 架構 = **支援 FP8**。這個數字決定 Phase 2 能做哪些量化實驗。

---

## Part 3：把 vLLM 跑起來

### 3.1 做法

先把 LoRA adapter 抓下來（vLLM 要絕對路徑，見陷阱）：

```bash
pip install --user huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'ed-donner/price-2025-11-28_18.47.07',
    revision='b19c8bfea3b6ff62237fbb0a8da9779fc12cefbd',
    local_dir='/home/ubuntu/adapter',
)
"
```

起 vLLM：

```bash
docker run --rm -d --name vllm \
  --gpus all \
  --ipc=host \
  -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora \
    --lora-modules pricer=/adapter \
    --max-lora-rank 256 \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85

docker logs -f vllm            # 等它說 "Application startup complete"
```

### 3.2 為什麼每個旗標都在那裡

| 旗標 | 為什麼 |
|---|---|
| `--ipc=host` | vLLM 用共享記憶體做行程間通訊。不設的話預設 `/dev/shm` 只有 64MB，**負載一上來就 crash**。（Phase 3 在 k8s 裡的對應是 `emptyDir: {medium: Memory}`） |
| `--gpus all` | 沒有這個容器裡看不到 GPU |
| `--max-lora-rank 256` | **我們的 adapter 是 r=256，預設值是 16。** 見 Part 5，這是本階段最重要的陷阱 |
| `--max-model-len 512` | prompt 約 150 + 輸出 5。預設是 131072，會把 KV cache 浪費掉 99% 以上。**這是本 workload 最大的效能槓桿**，Phase 2 會量化它的影響 |
| `--gpu-memory-utilization 0.85` | 預先劃給權重 + KV cache 的比例。0.9 是預設，留一點餘裕比較不會在尖峰爆掉 |
| `--served-model-name base` | 讓 base model 和 LoRA 有不同的名字，這樣同一個服務可以兩個都打 —— Part 6 的對照實驗要用 |

**啟動 log 裡最該看的那一行**：

```
# GPU blocks: 12345, # CPU blocks: 2048
```

這是 vLLM 劃給 KV cache 的 block 數。`--max-model-len` 調小之後這個數字會暴增，直接對應「能跑多少並發」。**Phase 2 調校時，這一行是你的儀表板。**

### 3.3 驗證

```bash
curl -s localhost:8000/v1/models | python3 -m json.tool
# 應該看到 "base" 和 "pricer" 兩個

curl -s localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "pricer",
    "prompt": "What does this cost to the nearest dollar?\n\nTitle: Bosch 12V Max Cordless Drill\nCategory: Tools\nBrand: Bosch\nDescription: A compact cordless drill for home use.\nDetails: Includes two batteries and a charger.\n\nPrice is $",
    "max_tokens": 5,
    "temperature": 0
  }' | python3 -c "import json,sys; print(repr(json.load(sys.stdin)['choices'][0]['text']))"
```

### ⚠️ 陷阱

- **`--lora-modules` 在容器裡必須用絕對路徑。相對路徑會靜靜失敗**，不報錯，然後你的估價全是 base model 的水準。
- **必須用 `/v1/completions`，不是 `/v1/chat/completions`。** base model 沒有 chat template，套 chat 格式的話 prompt 就不是訓練時的樣子。
- **回傳只有「續寫」部分，不含 prompt。** 課程 Modal 版的 `result.split("Price is $")[1]` 照抄會 IndexError —— 那是因為 HF 的 `generate()` 會把 prompt 一起回傳，vLLM 不會。
- **gated model 需要 `HF_TOKEN`**，而且要接受過授權。403 就是這個問題。
- **第一次啟動要下載 6.4GB 的權重**，加上 CUDA graph 編譯，總共 2–5 分鐘。這個時間就是 Phase 3 `startupProbe` 的依據 —— **現在就量一下並記下來**。

---

## Part 4：正確性驗證 —— 對齊 MAE $39.85

**這是本階段的核心。** 沒有這一步，後面所有效能數字都沒有意義。

### 4.1 做法

在**本機**跑（透過 SSH 通道）：

```bash
# 本機另一個終端
ssh -i <key> -N -L 8000:localhost:8000 ubuntu@<IP>

# 再另一個終端
cd ~/Desktop/side-projects/llm-inference-k8s-lab
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 \
  --model pricer \
  --size 200 \
  --label "phase1-vllm-bf16-lora-rank256"
```

### 4.2 判讀

| MAE | 代表什麼 | 下一步 |
|---|---|---|
| **$36 ~ $45** | ✅ 正確。LoRA 完整生效 | 記進 `RESULTS.md`，往 Part 5 走 |
| **$105 ~ $115** | ❌ LoRA 完全沒生效 | 檢查 `--lora-modules` 路徑是不是絕對路徑、`--model` 有沒有打成 `pricer` |
| **$50 ~ $80** | ⚠️ **部分生效** | 極可能是 `--max-lora-rank` 太小造成的截斷。Part 5 就是在講這個 |

**同時記下 base model 的分數當對照組**：

```bash
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 \
  --model base \
  --size 200 \
  --label "phase1-vllm-bf16-BASE-no-lora"
```

這應該落在 $110 左右（leaderboard 上「未微調的 Llama 3.2」是 $110.72）。**有了這個對照組，你就永遠分得清「LoRA 沒生效」和「LoRA 效果不好」。**

### 4.3 跟 Phase 0 的差異怎麼解釋

你現在有兩個數字：

| 環境 | 量化 | LoRA |
|---|---|---|
| Phase 0（Mac） | base Q8_0（llama.cpp） | GGUF f16 |
| Phase 1（GPU） | base **bf16**（無量化） | 原生 safetensors |

**Phase 1 的 MAE 應該略優於 Phase 0**，差距就是「Q8_0 量化的代價」。如果兩者幾乎一樣，代表 Q8_0 對這個任務幾乎無損 —— 那本身就是個有價值的結論（而且是你量出來的）。

**還有一個共同的變因值得記在文件裡**：這個 adapter 是用 **QLoRA（4-bit NF4 base）** 訓練的，但兩個環境都是在更高精度的 base 上推論。訓練與推論的量化不一致會造成漂移，這是 QLoRA 的已知特性，不是你的 bug。

---

## Part 5：刻意失敗 —— `--max-lora-rank` 的無聲截斷

**這是整個 lab 最好的一個教材。** 務必實際做一次。

### 5.1 做法

```bash
docker stop vllm

docker run --rm -d --name vllm \
  --gpus all --ipc=host -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora \
    --lora-modules pricer=/adapter \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85
    # ← 注意：這次「沒有」--max-lora-rank，用預設值
```

然後跑同一份評估：

```bash
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 --model pricer --size 200 \
  --label "phase1-vllm-bf16-DEFAULT-max-lora-rank"
```

### 5.2 你會觀察到什麼

**服務正常啟動。健康檢查通過。API 正常回應。回傳的是合法的價格數字。**

**但 MAE 是錯的。**

把兩組數字並排放進 `RESULTS.md`：

| 設定 | MAE | 服務有起來嗎 | API 有回應嗎 |
|---|---|---|---|
| `--max-lora-rank 256` | 約 $40 | ✅ | ✅ |
| 預設值（16） | **偏掉** | ✅ | ✅ |

### 5.3 為什麼這件事這麼重要

`--max-lora-rank` 的支援值是 8/16/32/64/128/256/320/512，**預設是 16**。我們的 adapter 是 **r=256**。

當 adapter 的 rank 超過設定值時，vLLM 會**載入它能載的部分**，而不是拒絕啟動。你得到一個「部分生效」的 LoRA。

**這證明了一件事：服務起來 ≠ 正確。**

所有的健康檢查、readiness probe、`/v1/models` 回應、HTTP 200 —— 全部都是綠的。只有**業務層面的正確性驗證**能抓到這個錯誤。

**面試答案的形狀**：
> 「我在部署 LoRA 時踩過一個坑：adapter 的 rank 是 256，但 vLLM 的 `--max-lora-rank` 預設是 16，超過的部分會被靜靜截斷。服務完全正常、API 也正常回應，但預測品質掉了。這件事讓我在 CI 裡加了一個 MAE 門檻檢查 —— 光有 health check 抓不到這種錯誤。」

### 5.4 這件事的通用啟示

**每個推論服務的部署流程都該有一道「業務正確性」的關卡**，而不只是 liveness / readiness。對這個 lab 來說就是：

```
部署 → health check 通過 → 跑 50 筆的 MAE smoke test → MAE < 門檻才放流量
```

這正是 Phase 3 可以用 k8s Job 實作的東西（在 rollout 之後跑一個評估 Job，失敗就 `rollout undo`）。

**記得把 `--max-lora-rank 256` 加回去再往下走。**

---

## Part 6：原創實驗 —— runtime LoRA vs merged 權重

**目的**：r=256 是很大的 rank，runtime LoRA 的 kernel 開銷不小。這是一個真實的工程取捨，而且沒有人幫你量過。

### 6.1 做法

先把 LoRA merge 進 base（在 GPU 機器上做，記憶體充裕）：

```bash
pip install --user torch transformers peft accelerate

python3 - <<'PY'
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-3.2-3B", torch_dtype=torch.bfloat16, device_map="cpu"
)
merged = PeftModel.from_pretrained(
    base, "/home/ubuntu/adapter"
).merge_and_unload()
merged.save_pretrained("/home/ubuntu/merged")
AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-3B").save_pretrained("/home/ubuntu/merged")
print("merge 完成")
PY
```

起一個 merged 版本的服務：

```bash
docker stop vllm
docker run --rm -d --name vllm \
  --gpus all --ipc=host -p 8000:8000 \
  -v /home/ubuntu/merged:/merged:ro \
  vllm/vllm-openai:latest \
    --model /merged \
    --served-model-name pricer \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85
```

兩邊都跑一次評估，**同時記 MAE 和吞吐**（吞吐的正式量測在 Phase 2，這裡先用 `measure.py` 的 `--workers 20` 抓個概數）。

### 6.2 取捨在哪

| | **Runtime LoRA** | **Merged 權重** |
|---|---|---|
| 記憶體 | 一份 base + N 個小 adapter | 每個 adapter 一份完整模型 |
| 速度 | 每層多一次低秩運算，**r 越大越貴** | 跟原生模型完全一樣 |
| 熱插拔 | ✅ 可以，多租戶共用一個 base | ❌ 換模型要重載整個服務 |
| 部署複雜度 | 低（adapter 只有幾百 MB） | 高（每次都要 merge + 推一份完整權重） |
| 適用 | 多 adapter、流量分散、需要彈性 | 單一 adapter、追求極致延遲 |

**r=256 讓這個取捨變得不明顯** —— 一般 r=8~32 的 adapter，runtime 的開銷小到可以忽略；r=256 就未必了。**這就是為什麼要量。**

### 6.3 驗證

成功條件：**兩邊的 MAE 應該幾乎相同**（差異在小數點後），因為數學上是等價的。如果差很多，代表 merge 或載入有問題。

吞吐則可能有明顯差異 —— 那就是你的實驗結果。填進 `RESULTS.md`。

### ⚠️ 陷阱

- **merge 出來的模型約 6.4GB，磁碟要夠。**
- **merge 要用跟訓練時相同的 dtype 概念思考**：adapter 是 QLoRA（4-bit base）訓的，merge 到 bf16 base 上會有些微漂移。這跟 Part 4 講的是同一件事。
- **merged 版本沒有 `--enable-lora`**，所以 `--max-lora-rank` 那個坑自然消失 —— 這本身就是 merged 的一個優點。

---

## Part 7：四方對照表

把到目前為止的所有數字整理成一張表填進 `RESULTS.md`：

| # | 環境 | 權重精度 | LoRA 方式 | MAE | 備註 |
|---|---|---|---|---|---|
| 0 | leaderboard 基準 | — | — | **$39.85** | 課程公開數字，驗收門檻 |
| 1 | leaderboard（未微調） | 4-bit | 無 | $110.72 | 「LoRA 沒生效」的指紋 |
| 2 | Mac / llama.cpp | Q8_0 | GGUF f16 runtime | ? | Phase 0 |
| 3 | GPU / vLLM | bf16 | runtime, rank 256 | ? | Phase 1 主線 |
| 4 | GPU / vLLM | bf16 | runtime, **預設 rank 16** | ? | 刻意失敗的對照 |
| 5 | GPU / vLLM | bf16 | **merged** | ? | 應與 #3 幾乎相同 |
| 6 | GPU / vLLM | bf16 | 無（base） | ? | 應接近 #1 |

**這張表就是你的面試素材。** 它證明的不是「我會部署 vLLM」，而是「我會設計對照實驗、我知道每個數字為什麼是那樣」。

---

## Phase 1 驗收清單

- [ ] `nvidia-smi` 看得到 L4，`compute_cap` 是 8.9
- [ ] vLLM 的 `/v1/models` 同時列出 `base` 與 `pricer`
- [ ] 主線 MAE 落在 $36–$45
- [ ] base model 的對照組 MAE 落在 $105–$115
- [ ] **`--max-lora-rank` 截斷實驗的兩組數字都記錄了**
- [ ] merged vs runtime LoRA 的 MAE 幾乎相同（驗證 merge 正確）
- [ ] 已量並記下 vLLM 的**冷啟動時間**（Phase 3 的 `startupProbe` 要用）
- [ ] 啟動 log 的 `# GPU blocks` 數字已記下（Phase 2 調校的基準）
- [ ] 對照表七行都填完
- [ ] **機器已 `stop`**

---

## 收工（每次離開電腦都要做）

```bash
aws ec2 stop-instances --region us-east-1 --instance-ids <ID>

# 確認真的停了
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]' --output table
```

**空的表格 = 安全。** 有東西 = 立刻停掉。

---

## 下一站

→ [`05-phase2-tuning.md`](05-phase2-tuning.md)：從「跑得起來」進到「知道瓶頸在哪」。壓測、調校、量化對照表 —— 這是面試被追問時的分水嶺。
